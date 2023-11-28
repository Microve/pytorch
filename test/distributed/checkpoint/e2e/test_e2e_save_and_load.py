# Owner(s): ["oncall: distributed"]

from enum import auto, Enum

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as DCP
import torch.nn as nn
from torch.distributed._tensor.device_mesh import init_device_mesh
from torch.distributed.checkpoint.state_dict import (
    _patch_model_state_dict,
    _patch_optimizer_state_dict,
    get_state_dict,
)
from torch.distributed.distributed_c10d import ReduceOp
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.api import ShardingStrategy
from torch.distributed.tensor.parallel import PairwiseParallel, parallelize_module
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    run_tests,
)

from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    skip_if_lt_x_gpu,
    with_comms,
)
from torch.testing._internal.distributed.checkpoint_utils import with_temp_dir
from torch.testing._internal.distributed.common_state_dict import VerifyStateDictMixin


# Simple and boring model
class TestDummyModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        torch.manual_seed(0)
        self.net1 = nn.Sequential(nn.Linear(8, 16), nn.ReLU())
        self.net2 = nn.Sequential(nn.Linear(16, 32), nn.ReLU())
        self.net3 = nn.Sequential(nn.Linear(32, 64), nn.ReLU())
        self.net4 = nn.Sequential(nn.Linear(64, 8), nn.ReLU())

    def forward(self, x):
        return self.net4(self.net3(self.net2(self.net1(x))))

    def get_input(self):
        return torch.rand(8, 8, device="cuda")


class TestStatefulObj:
    def __init__(self):
        self.data = torch.rand(10, 10, device="cuda")

    def state_dict(self):
        return {"data": self.data}

    def load_state_dict(self, state_dict):
        self.data = state_dict["data"]

    def __eq__(self, other):
        return torch.equal(self.data, other.data)


class ModelType(Enum):
    FSDP = auto()
    HSDP = auto()
    FSDP_TP = auto()
    NONE = auto()  # no parallelization


def _train(model, optim, train_steps=1):
    torch.manual_seed(0)
    loss = None
    for _ in range(train_steps):
        loss = model(model.get_input()).sum()
        loss.backward()
        optim.step()
        optim.zero_grad()

    return loss


class TestE2ELoadAndSave(DTensorTestBase, VerifyStateDictMixin):
    def _create_model(self, compile, model_type, train_steps=2):
        dummy_model = TestDummyModel().cuda()

        assert model_type in ModelType, f"{model_type} is not supported."
        if model_type == ModelType.FSDP:
            device_mesh = init_device_mesh(self.device_type, (self.world_size,))
            model = FSDP(
                dummy_model,
                device_mesh=device_mesh,
                use_orig_params=True,
            )
        elif model_type == ModelType.HSDP:
            device_mesh = init_device_mesh(self.device_type, (2, self.world_size // 2))
            model = FSDP(
                dummy_model,
                device_mesh=device_mesh,
                use_orig_params=True,
                sharding_strategy=ShardingStrategy.HYBRID_SHARD,
            )
        elif model_type == ModelType.FSDP_TP:
            mesh_2d = init_device_mesh(
                self.device_type, (2, self.world_size // 2), mesh_dim_names=("dp", "tp")
            )
            tp_mesh = mesh_2d["tp"]
            dp_mesh = mesh_2d["dp"]
            model = parallelize_module(dummy_model, tp_mesh, PairwiseParallel())
            model = FSDP(model, device_mesh=dp_mesh, use_orig_params=True)
        else:
            model = dummy_model

        if compile:
            model = torch.compile(model)

        optim = self._optim(model)
        if model_type is not ModelType.NONE:
            _patch_model_state_dict(model)
            _patch_optimizer_state_dict(model, optimizers=optim)

        return model, optim

    def _optim(self, model):
        return torch.optim.Adam(model.parameters(), lr=0.1)

    @with_comms
    @skip_if_lt_x_gpu(4)
    @with_temp_dir
    @parametrize("compile", [True, False])
    @parametrize("model_type", [ModelType.FSDP, ModelType.HSDP, ModelType.FSDP_TP])
    def test_e2e(self, compile, model_type):
        model, optim = self._create_model(compile, ModelType.NONE)
        _train(model, optim, train_steps=2)

        dist_model, dist_optim = self._create_model(compile, model_type)
        _train(dist_model, dist_optim, train_steps=2)

        original_stateful_obj = TestStatefulObj()  # tests arbitrary saving/loading
        DCP.save(
            state_dict={
                "model": dist_model,
                "optimizer": dist_optim,
                "s": original_stateful_obj,
            },
            storage_writer=DCP.FileSystemWriter(self.temp_dir),
        )

        loaded_stateful_obj = TestStatefulObj()
        dist_model, dist_optim = self._create_model(compile, model_type)
        DCP.load(
            state_dict={
                "model": dist_model,
                "optimizer": dist_optim,
                "s": loaded_stateful_obj,
            },
            storage_reader=DCP.FileSystemReader(self.temp_dir),
        )

        self.assertEqual(original_stateful_obj, loaded_stateful_obj)

        # train one more step on both models
        loss = _train(model, optim, train_steps=1)
        dist_loss = _train(dist_model, dist_optim, train_steps=1)
        self.assertEqual(loss, dist_loss)

        dist_msd, dist_osd = get_state_dict(dist_model, optimizers=dist_optim)
        model_sd, optim_sd = get_state_dict(model, optimizers=optim)

        self._verify_msd(model_sd, dist_msd)
        self._verify_osd_by_load(model, optim, self._optim(model), dist_osd)

    @with_comms
    @with_temp_dir
    @skip_if_lt_x_gpu(4)
    def test_different_ordered_state_dict_keys(self):
        """Tests that the order of keys in the state dict does not matter when loading
        If order was not accounted for, the following test would cause a deadlock.
        """

        world_size = self.world_size

        class Foo:
            def state_dict(self):
                return {}

            def load_state_dict(self, state_dict):
                tl = [
                    torch.ones(2, dtype=torch.int64, device="cuda")
                    for _ in range(world_size)
                ]
                t = (
                    torch.arange(2, dtype=torch.int64, device="cuda")
                    + 1
                    + 2 * dist.get_rank()
                )
                dist.all_gather(tl, t, async_op=False)

        class Bar:
            def state_dict(self):
                return {}

            def load_state_dict(self, state_dict):
                tensor = (
                    torch.arange(2, dtype=torch.int64, device="cuda")
                    + 1
                    + 2 * dist.get_rank()
                )
                dist.all_reduce(tensor, op=ReduceOp.SUM)

        if self.rank == 0:
            sd = {
                "A": Foo(),
                "B": Bar(),
            }
        else:
            sd = {
                "B": Bar(),
                "A": Foo(),
            }

        DCP.save(sd, DCP.FileSystemWriter(self.temp_dir))
        DCP.load(sd, DCP.FileSystemReader(self.temp_dir))


instantiate_parametrized_tests(TestE2ELoadAndSave)
if __name__ == "__main__":
    run_tests()
