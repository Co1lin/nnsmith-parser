import tvm
from tvm import relay
from multipledispatch import dispatch

from nnsmith.backends import BackendFactory
from nnsmith.materialize.onnx import ONNXModel


def list_eq(a, b):
    if len(a) != len(b):
        return False
    for i in range(len(a)):
        if a[i] != b[i]:
            return False
    return True


class TVMFactory(BackendFactory):
    def __init__(self, device="cpu", optmax=True, executor="graph", **kwargs) -> None:
        super().__init__(device, optmax, **kwargs)
        # WARNING: setting opt_level 4 sometimes causes false alarms
        # as in this level fast_math is enabled where slight numerical
        # inconsistency is allowed and outputs for UB-input may differ.
        self.opt_level = 4 if optmax else 0
        self.target = tvm.target.Target("llvm" if device == "cpu" else "cuda")
        self.executor_mode = executor

    def get_device(self):
        if self.target.export()["kind"] == "cuda":
            return tvm.cuda()
        if self.target.export()["kind"] == "rocm":
            return tvm.rocm()
        return tvm.cpu()

    @property
    def system_name(self) -> str:
        return "tvm"

    @staticmethod
    def cvt_result(output):
        """Pack output tensor(s) into a list"""
        # TODO(jinkun): may not work for nested list / dynamic shape
        assert output is not None, "Output should not be None"
        if isinstance(output, (tvm.runtime.container.ADT, list)):
            output = [r.numpy() for r in output]
        elif output is not None:
            output = [output.numpy()]
        return output

    @dispatch(ONNXModel)
    def mk_backend(self, model: ONNXModel):
        onnx_model = model.native_model
        shape_dict = {name: aten.shape for name, aten in model.input_like.items()}
        mod, params = relay.frontend.from_onnx(
            onnx_model, shape_dict, freeze_params=True
        )
        mod = relay.transform.InferType()(mod)

        with tvm.transform.PassContext(opt_level=self.opt_level):
            executor = relay.build_module.create_executor(
                self.executor_mode, mod, self.get_device(), self.target, params
            ).evaluate()

        def closure(inputs):
            output = executor(**inputs)
            output = self.cvt_result(output)
            return dict(zip(model.output_like.keys(), output))

        return closure