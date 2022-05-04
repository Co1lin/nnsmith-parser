import random
import pickle
from pathlib import Path
from typing import Dict
import time

import numpy as np
from tqdm import tqdm
import onnx
import onnx.checker

from nnsmith import difftest, input_gen
from nnsmith.backends import DiffTestBackend


class CrashExecutor(DiffTestBackend):
    """For testing purposes"""

    def predict(self, *args, **kwargs):
        assert False


class HangExecutor(DiffTestBackend):
    """For testing purposes"""

    def predict(self, *args, **kwargs):
        while True:
            pass


class DummyExecutor(DiffTestBackend):
    """Doing nothing"""

    def predict(self, *args, **kwargs):
        return {}


class BackendCreator:
    NAME_MAP = {
        'ort': 'ORTExecutor',
        'ort-debug': 'ORTExecutorDebug',
        'tvm-llvm': 'TVMExecutorLLVM',
        'tvm-debug': 'TVMExecutorDebug',
        'tvm-cuda': 'TVMExecutor',
        'xla': 'XLAExecutor',
        'trt': 'TRTBackend',
    }

    def __init__(self, name):
        self.name = name
        self.dump_name = self.NAME_MAP[name]

    def __call__(self, *args, **kwargs):
        name = self.name
        if name == 'ort':
            from nnsmith.backends.ort_graph import ORTExecutor
            return ORTExecutor()
        elif name == 'ort-debug':
            from nnsmith.backends.ort_graph import ORTExecutor
            return ORTExecutor(0)
        elif name == 'tvm-debug':
            from nnsmith.backends.tvm_graph import TVMExecutor
            return TVMExecutor(executor='debug', opt_level=0)
        elif name == 'tvm-llvm':
            from nnsmith.backends.tvm_graph import TVMExecutor
            return TVMExecutor(target='llvm')
        elif name == 'tvm-cuda':
            from nnsmith.backends.tvm_graph import TVMExecutor
            return TVMExecutor(target='cuda')
        elif name == 'xla':
            from nnsmith.backends.xla_graph import XLAExecutor
            return XLAExecutor(device='CUDA')
        elif name == 'trt':
            from nnsmith.backends.trt_graph import TRTBackend
            return TRTBackend()
        elif name == 'crash':
            return CrashExecutor()
        elif name == 'hang':
            return HangExecutor()
        else:
            raise ValueError(f'unknown backend: {name}')


def summarize(outputs: Dict[str, np.ndarray]):
    m = {k + '_mean': np.mean(o) for k, o in outputs.items()}
    # TODO(JK): figure out how to deal with nan
    m.update({k + '_nanmean': np.nanmean(o) for k, o in outputs.items()})
    m.update({k + '_num_nan': np.sum(np.isnan(o)) for k, o in outputs.items()})
    return m


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=str, required=True,
                        help=f'One of {BackendCreator.NAME_MAP.keys()}')
    parser.add_argument('--model', type=str,
                        help='For debugging purpose: path to onnx model;')
    parser.add_argument(
        '--dump_raw', help='Dumps the raw output to the specified path')
    parser.add_argument('--raw_input', type=str,
                        help='When specified, the model will be fed with the specified input. Otherwise, input will be generated on the fly.')
    parser.add_argument('--oracle', type=str, help='Path to the oracle')
    parser.add_argument('--seed', type=int,
                        help='to generate random input data')
    parser.add_argument('--cmp_with', type=str, default=None,
                        help='the backend to compare with')

    # TODO: Add support for passing backend-specific options
    args = parser.parse_args()

    st = time.time()
    if args.seed is None:
        seed = random.getrandbits(32)
    else:
        seed = args.seed
    print('Using seed:', seed)

    onnx_model = onnx.load(args.model)
    onnx.checker.check_model(onnx_model, full_check=True)

    # Step 1: Generate input
    oracle = None
    oracle_outputs = None
    # -- oracle:
    if args.oracle is not None:
        print('Using oracle from:', args.oracle)
        test_inputs, oracle_outputs = pickle.load(Path(args.oracle).open('rb'))
    # -- raw_input:
    else:
        if args.raw_input is not None:
            print('Using raw input pkl file from:', args.raw_input)
            test_inputs = pickle.load(Path(args.raw_input).open('rb'))
        # -- randomly generated input:
        else:
            print('No raw input or oracle found. Generating input on the fly.')
            inp_spec = DiffTestBackend.analyze_onnx_io(onnx_model)[0]
            test_inputs = input_gen.gen_one_input_rngs(inp_spec, None, seed)

    # Step 2: Run backend
    # -- reference backend:
    if args.cmp_with is not None:
        print(f'Using {args.cmp_with} as the reference backend/oracle')
        reference_backend = BackendCreator(args.cmp_with)()
        oracle_outputs = reference_backend.predict(onnx_model, test_inputs)
        if input_gen.is_invalid(oracle_outputs):
            print(
                f'[WARNING] Backend {args.cmp_with} produces nan/inf in output.')

    # -- this backend:
    this_backend = BackendCreator(args.backend)()
    this_outputs = this_backend.predict(onnx_model, test_inputs)
    if input_gen.is_invalid(this_outputs):
        print(f'[WARNING] Backend {args.backend} produces nan/inf in output.')

    # Step 3: Compare
    if oracle_outputs is not None:
        difftest.assert_allclose(this_outputs, oracle_outputs,
                                 args.backend, args.cmp_with if args.cmp_with else "oracle")
        print('Differential testing passed!')

    if args.dump_raw is not None:
        print('Storing (input,output) pair to:', args.dump_raw)
        pickle.dump((test_inputs, this_outputs), open(args.dump_raw, 'wb'))

    print(f'Total time: {time.time() - st}')
