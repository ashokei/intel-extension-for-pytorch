from __future__ import print_function

import argparse
import collections
import lark
import os
import re
import string
import sys
import json

from common.codegen import write_or_skip
from common.cpp_sig_parser import CPPSig
from common.aten_sig_parser import AtenSig
import common.utils as utils
from common.native_functions import NativeFunctions

_FN_BYPASS_REGEX = [
    # ATEN CUDA functions
    r'[^(]*cudnn',
    r'[^(]*cufft',
    r'[^(]*mkldnn',
    r'[^(]*_amp',
    r'[^(]*_test_',
]

_SHALLOW_FALLBACK_TO_CPU_TENSOR_LIST = 'shallowFallbackToCPUTensorList'
_SHALLOW_FALLBACK_TO_CPU_TENSOR = 'shallowFallbackToCPUTensor'
_SHALLOW_UPGRADE_TO_DPCPP_TENSOR = 'shallowUpgradeToDPCPPTensor'
_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_VEC = 'shallowUpgradeToDPCPPTensorVec'
_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_A = 'shallowUpgradeToDPCPPTensorA'
_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_AW = 'shallowUpgradeToDPCPPTensorAW'

_REG_PATTERN =  """
    m.impl("{}", static_cast<{}>(&{}));"""

_REG_BLOCK = """
namespace {{
  TORCH_LIBRARY_IMPL(aten, SparseXPU, m) {{
    {reg_ops}
  }}
}}"""


_H_HEADER = """// Autogenerated file by {gen}. Do not edit directly!
#pragma once

#include <ATen/ATen.h>

namespace torch_ipex {{
namespace cpu {{

class AtenIpexCPUSparse {{
 public:
{hfuncs}
}};

}}  // namespace cpu

}}  // namespace torch_ipex
"""

_CPP_HEADER = """// Autogenerated file by {gen}. Do not edit directly!
#include "SparseOPs.h"

#include <ATen/SparseTensorUtils.h>
#include <ATen/SparseTensorImpl.h>
#include <ATen/core/op_registration/op_registration.h>
#include <ATen/record_function.h>
#include <c10/util/Exception.h>
#include <c10/util/Logging.h>
#include <torch/csrc/autograd/function.h>
#include <torch/library.h>

#include "aten_ipex_bridge.h"
#include "ipex_sparse_tensor_impl.h"

namespace torch_ipex {{
namespace cpu {{

{funcs}

{regs}

}}  // namespace cpu
}}  // namespace torch_ipex
"""

_RESULT_NAME = '_ipex_result'
_IPEX_OP_FUNC_NS = 'AtenIpexCPUSparse'

class SparseOPCodeGen(object):
    def __init__(self, reg_dec_file_path, func_file_path, sparse_dec_file_path, sparse_attr_file_path, op_h_file_path, op_cpp_file_path):
        self._reg_dec_file_path = reg_dec_file_path
        self._func_file_path = func_file_path
        self._sparse_dec_file_path = sparse_dec_file_path
        self._sparse_attr_file_path = sparse_attr_file_path
        self._op_h_file_path = op_h_file_path
        self._op_cpp_file_path = op_cpp_file_path
        self._sigs = []
        self._sparse_attr_data = ''
        self._sparse_sigs = []
        self._err_info = []
        self._native_funcs = NativeFunctions(func_file_path)

    def is_sparse_attr_function(self, func_name):
        if self._sparse_attr_data.find(' {}('.format(func_name)) >= 0:
            return True
        else:
            return False

    def is_void_func(self, cpp_sig):
        ret_params = cpp_sig.ret_params
        assert len(ret_params) == 1
        ret_param = ret_params[0]
        if ret_param.core_type == 'void' and not ret_param.is_pointer:
            return True
        return False

    def is_bypass_func(self, cpp_sig):
        for frx in _FN_BYPASS_REGEX:
            if re.match(frx, cpp_sig.def_name):
                return True
        return False

    def cross_correct_sig(self, cpp_sig, aten_sig):
        for cpp_input_param in cpp_sig.input_params:
            for aten_sig_param in aten_sig.input_params:
                if cpp_input_param.name == aten_sig_param.name:
                    cpp_input_param.is_to_be_written = aten_sig_param.is_to_be_written
                    cpp_input_param.is_alias = aten_sig_param.is_alias

    def prepare_functions(self):
        # Parse SparseCPUType.h
        _sparse_sig_strs = []
        for line in open(self._sparse_dec_file_path, 'r'):
            m = re.match(r'\s*([^\s].*\));', line)
            if not m:
                continue
            cpp_func_sig_str = m.group(1)
            _sparse_sig_strs.append(cpp_func_sig_str)
        #     print(cpp_func_sig_str)
        # print("********************")

        # Parse SparseAttrType.h
        with open(self._sparse_attr_file_path, 'r') as ff:
            self._sparse_attr_data = ff.read()

        # Parse Functions.h
        with open(self._func_file_path, 'r') as ff:
            self._func_data = ff.read()

        # Parse Registration declartion.h
        for line in open(self._reg_dec_file_path, 'r'):
            m = re.match(r'\s*([^\s].*); //\s+(.*)', line)
            if not m:
                continue
            cpp_func_sig = m.group(1).replace('at::', '').replace('c10::', '')
            aten_func_sig_literal = m.group(2)

            aten_func_sig = aten_func_sig_literal
            if "schema" in aten_func_sig_literal and "dispatch" in aten_func_sig_literal:
                res = json.loads(aten_func_sig_literal)
                aten_func_sig = res["schema"]

            if not utils.is_tensor_api(cpp_func_sig):
                continue

            try:
                for sparse_cpp_sig_str in _sparse_sig_strs:
                    if sparse_cpp_sig_str.find("clone") >= 0 and cpp_func_sig.find("clone") >= 0:
                        print("{} {}".format(sparse_cpp_sig_str, cpp_func_sig))

                    if sparse_cpp_sig_str.replace(' ', '') == cpp_func_sig.replace(' ', ''):
                        sparse_sig = CPPSig(sparse_cpp_sig_str)
                        sparse_sig.is_tensor_member_func = self._native_funcs.is_tensor_member_function(sparse_sig.def_name)

                        native_cpp_sig = None
                        if utils.is_out_func(sparse_sig.def_name):
                            native_cpp_sig = self._native_funcs.query(sparse_sig)

                        aten_sig = AtenSig(aten_func_sig)

                        self.cross_correct_sig(sparse_sig, aten_sig)

                        self._sigs.append((sparse_sig, aten_sig, native_cpp_sig, sparse_cpp_sig_str, aten_func_sig))
                    else:
                        continue
            except Exception as e:
                self._err_info.append((cpp_func_sig, str(e)))
                print('Error parsing "{}": {}'.format(cpp_func_sig, e), file=sys.stderr)

        print('Extracted {} functions ({} errors) from {}'.format(
              len(self._sigs),
              len(self._err_info),
              self._reg_dec_file_path),
            file=sys.stderr)
        assert len(self._err_info) == 0

    def get_alias_tensor_by_index(self, cpp_sig, idx):
        alias_tensors = cpp_sig.get_alias_tensors()
        assert len(alias_tensors) > idx
        return alias_tensors[idx]

    def get_ret_type_str(self, cpp_func_str):
        cpp_func_str = utils.add_ns(cpp_func_str)

        m = re.search(r'(.*) (\b\S*)\(', cpp_func_str)
        assert m
        return m.group(1)

    def get_func_dec(self, cpp_sig):
        func_dec_str = cpp_sig.sig_str.replace(cpp_sig.def_name + '(', ' (*)(')
        return utils.add_ns(func_dec_str)

    def gen_func_signature(self, cpp_func_str, old_func_name, new_func_name):
        cpp_func_str_h = utils.add_ns(cpp_func_str.replace(old_func_name + '(', new_func_name + '('))
        func_name_with_ns = "{}::{}".format(_IPEX_OP_FUNC_NS, new_func_name)
        cpp_func_str_cpp = cpp_func_str_h.replace(new_func_name + '(', func_name_with_ns + '(')

        return cpp_func_str_h, cpp_func_str_cpp

    def gen_fallback_prepare_code(self, cpp_sig):
        code = ''
        op_check_code = ''
        for param in cpp_sig.input_params:
            if param.core_type == 'TensorList':
                ipex_name = '_ipex_{}'.format(param.name)
                code += ('  auto&& {} = bridge::{}({});\n').format(ipex_name, _SHALLOW_FALLBACK_TO_CPU_TENSOR_LIST, param.name)
                param.ipex_name = ipex_name
            elif param.core_type == 'TensorOptions':
                ipex_name = '_ipex_{}'.format(param.name)
                param.ipex_name = ipex_name
                check_cond = '{}.device().type() == at::DeviceType::XPU'.format(param.name)
                op_check_code += '  TORCH_INTERNAL_ASSERT_DEBUG_ONLY({});\n'.format(check_cond)
                code += '  at::TensorOptions {} = {}.device(at::DeviceType::CPU);\n'.format(ipex_name, param.name)
            elif param.core_type == 'Storage':
                code += '  TORCH_INTERNAL_ASSERT_DEBUG_ONLY({}.device_type() == c10::DeviceType::XPU);\n'.format(param.name)
            elif param.core_type == 'MemoryFormat':
                None
            elif param.core_type != 'Tensor':
                None
            # Tensor
            else:
                assert param.core_type == 'Tensor'
                ipex_name = '_ipex_{}'.format(param.name)
                code += '  auto&& {} = bridge::{}({});\n'.format(ipex_name, _SHALLOW_FALLBACK_TO_CPU_TENSOR, param.name)
                param.ipex_name = ipex_name
        return op_check_code + code

    def gen_fallback_code(self, cpp_sig, native_cpp_sig):
        for param in cpp_sig.input_params:
            assert param.name

        if native_cpp_sig is None:
            params_name = [param.ipex_name if param.ipex_name != '' else param.name for param in cpp_sig.input_params]
        else:
            params1_name = [param.name for param in cpp_sig.input_params]
            params2_name = [param.name for param in native_cpp_sig.input_params]
            new_idxs = utils.reorder_params_idx(params1_name, params2_name)
            input_params = cpp_sig.input_params
            params_name = [input_params[new_idxs[idx]].ipex_name if input_params[new_idxs[idx]].ipex_name != '' else input_params[new_idxs[idx]].name for idx in range(len(new_idxs))]

        code = ''
        start_idx, end_idx = utils.query_tensor_options(cpp_sig.input_params)
        if start_idx >= 0 and end_idx > start_idx:
            # assert bool((end_idx - start_idx + 1) == 4)
            wrapped_options = 'ipex_wrapped_options'
            code += '  auto&& {} = at::TensorOptions().dtype(dtype).device(at::DeviceType::CPU).layout(layout).pinned_memory(pin_memory);\n'
            code = code.format(wrapped_options)
            # Remove original param name
            params_name = params_name[:start_idx] + [wrapped_options] + params_name[end_idx + 1:]

        if cpp_sig.is_tensor_member_func:
            assert "_ipex_self" in params_name
            params_name.remove('_ipex_self')
            if self.is_void_func(cpp_sig):
                code += '  {}.{}({});\n'.format('_ipex_self', cpp_sig.def_name, ', '.join(params_name))
            else:
                code += '  auto&& {} = {}.{}({});\n'.format(_RESULT_NAME, '_ipex_self', cpp_sig.def_name, ', '.join(params_name))
        else:
            if self.is_void_func(cpp_sig):
                code += '  at::{}({});\n'.format(cpp_sig.def_name, ', '.join(params_name))
            else:
                code += '  auto&& {} = at::{}({});\n'.format(_RESULT_NAME, cpp_sig.def_name, ', '.join(params_name))
        return code

    def gen_fallback_post_code(self, cpp_sig):
        code = ''

        if self.is_void_func(cpp_sig):
            for param in cpp_sig.get_output_tensors():
                if param.is_tensor:
                    code += '  bridge::{}({}, {});\n'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_AW,
                                                             param.name,
                                                             param.ipex_name)
            return code

        # current OP is in-place or out OP
        if cpp_sig.contain_output_tensor:
            #assert cpp_sig.def_name.endswith('_') or cpp_sig.def_name.endswith('out')
            for param in cpp_sig.input_params:
                if param.is_tensor and param.is_to_be_written:
                    code += '  bridge::{}({}, {});\n'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_AW,
                                                             param.name,
                                                             param.ipex_name)

        ret_params = cpp_sig.ret_params
        assert len(ret_params) == 1
        ret_param = ret_params[0]
        if ret_param.core_type == 'std::tuple':
            assert len(ret_param.sub_params) > 0
            tuple_items = []
            for i, sub_param in enumerate(ret_param.sub_params):
                tuple_item = 'std::get<{}>({})'.format(i, _RESULT_NAME)
                tuple_item_final_str = tuple_item
                if sub_param.core_type == 'Tensor':
                    if sub_param.is_ref:
                        i_th_alias_tensor = self.get_alias_tensor_by_index(cpp_sig, i)
                        assert i_th_alias_tensor.name
                        tuple_item_final_str = i_th_alias_tensor.name
                    else:
                        tuple_item_final_str = 'bridge::{}({})'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR, tuple_item)

                tuple_items.append(tuple_item_final_str)

            code += '  static_cast<void>({}); // Avoid warnings in case not used\n'.format(_RESULT_NAME)
            code += '  return {}({});\n'.format(self.get_ret_type_str(cpp_sig.sig_str), ', '.join(tuple_items))
            return code

        if ret_param.core_type == 'std::vector':
            code += '  static_cast<void>({}); // Avoid warnings in case not used\n'.format(_RESULT_NAME)
            code += '  return bridge::{}({});\n'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_VEC, _RESULT_NAME)
            return code

        if ret_param.core_type == 'Tensor':
            code += '  static_cast<void>({}); // Avoid warnings in case not used\n'.format(_RESULT_NAME)

            if cpp_sig.contain_output_tensor:
                output_params = cpp_sig.get_output_tensors()
                assert len(output_params) == 1
                code += '  return {};\n'.format(output_params[0].name)
                return code
            else:
                if cpp_sig.contain_alias_tensor:
                    alias_tensors = cpp_sig.get_alias_tensors()
                    assert len(alias_tensors) == 1
                    alias_tensor = alias_tensors[0]
                    assert alias_tensor.name
                    assert alias_tensor.ipex_name
                    code += '  bridge::{}({}, {});\n'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR_A, alias_tensor.name, alias_tensor.ipex_name)
                code += '  return bridge::{}({});\n'.format(_SHALLOW_UPGRADE_TO_DPCPP_TENSOR, _RESULT_NAME)
                return code

        # Else: other return types
        code += '  static_cast<void>({}); // Avoid warnings in case not used\n'.format(_RESULT_NAME)
        code += '  return {};\n'.format(_RESULT_NAME)
        return code

    def gen_head_dec_code(self, cpp_func_str_h):
        return '  static {};\n'.format(cpp_func_str_h)

    def gen_cpu_ops_shard(self, func_defs, cpp_path, header_path, num_shards=1):
        head_file_content = _H_HEADER.format(gen=os.path.basename(sys.argv[0]), hfuncs=''.join([f['dec'] for f in func_defs]))
        write_or_skip(header_path, head_file_content)

        shards = [[] for _ in range(num_shards)]
        for idx, func in enumerate(func_defs):
            shards[idx % num_shards].append(func)

        for idx, shard in enumerate(shards):
            regs_code = _REG_BLOCK.format(reg_ops=''.join([f['reg'] for f in shard]))
            defs_code = ''.join([f['def'] for f in shard])

            filename, ext = os.path.splitext(cpp_path)
            shard_filepath = '%s_%s%s' % (filename, idx, ext)
            shard_content = _CPP_HEADER.format(gen=os.path.basename(sys.argv[0]), funcs=defs_code, regs=regs_code)
            write_or_skip(shard_filepath, shard_content)

    def gen_code(self):
        self.prepare_functions()
        assert len(self._err_info) == 0

        func_defs = []
        for cpp_sparse_sig, aten_sig, native_cpp_sig, cpp_sparse_func_sig_str, aten_func_sig_str in self._sigs:
            # The operator name should be unique because the new registration mechanism of PyTorch 1.7
            new_cpp_func_name = aten_sig.def_name.replace('.', '_')

            # Gen declaration code for head file
            cpp_func_str_h, cpp_func_str_cpp = self.gen_func_signature(cpp_sparse_func_sig_str, cpp_sparse_sig.def_name, new_cpp_func_name)
            func_dec = self.gen_head_dec_code(cpp_func_str_h)

            func_reg = _REG_PATTERN.format(aten_sig.def_name, self.get_func_dec(cpp_sparse_sig), "AtenIpexCPUSparse::" + new_cpp_func_name)

            code = ''
            # Since we have pre-defined attr OPs, we don't need to regenerate it
            if not self.is_sparse_attr_function(cpp_sparse_sig.def_name):

                # Gen definition code for cpp file
                code += '{} {{\n'.format(cpp_func_str_cpp)

                # Gen OP Name
                code += '#if defined(IPEX_DISP_OP)\n'
                code += '  printf("{}::{}\\n");\n'.format(_IPEX_OP_FUNC_NS, cpp_sparse_sig.def_name)
                code += '#endif\n'

                # Gen profile info
                profiler_inputs = []
                for param in cpp_sparse_sig.input_params:
                    if param.core_type in ['Tensor', 'Scalar']:
                        profiler_inputs.append(param.name)
                code += '#if defined(IPEX_PROFILE_OP)\n'
                code += '  RECORD_FUNCTION("{ns}::{name}", std::vector<c10::IValue>({{{input_names}}}));\n'.format(ns=_IPEX_OP_FUNC_NS, name=cpp_sparse_sig.def_name, input_names='')
                code += '#endif\n'

                code += self.gen_fallback_prepare_code(cpp_sparse_sig)
                code += self.gen_fallback_code(cpp_sparse_sig, native_cpp_sig)
                code += self.gen_fallback_post_code(cpp_sparse_sig)

                code += '}\n\n'

            func_defs.append({'dec': func_dec, 'reg': func_reg, 'def': code})

        self.gen_cpu_ops_shard(func_defs,
                               cpp_path=self._op_cpp_file_path,
                               header_path=self._op_h_file_path,
                               num_shards=1)

if __name__ == '__main__':
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        'ipex_cpu_ops_head',
        type=str,
        metavar='IPEX_CPU_OPS_HEAD_FILE',
        help='The path to the IPEX cpu ATEN overrides head file')
    arg_parser.add_argument(
        'ipex_cpu_ops_cpp',
        type=str,
        metavar='IPEX_CPU_OPS_CPP_FILE',
        help='The path to the IPEX cpu ATEN overrides cpp file')
    arg_parser.add_argument(
        'reg_dec',
        type=str,
        metavar='REG_DEC_FILE',
        help='The path to the RegistrationDeclarations.h file')
    arg_parser.add_argument(
        'functions',
        type=str,
        metavar='FUNCTIONS_FILE',
        help='The path to the Functions.h file')
    arg_parser.add_argument(
        'sparse_cpu_def_ops',
        type=str,
        metavar='SPARSE_CPU_DEF_OPS_FILE',
        help='The path to the SparseCPUType.h file')
    arg_parser.add_argument(
        'sparse_cpu_attr_ops',
        type=str,
        metavar='SPARSE_CPU_ATTR_OPS_FILE',
        help='The path to the SparseAttrs.h file')
    args, files = arg_parser.parse_known_args()
    sparse_code_gen = SparseOPCodeGen(
        args.reg_dec,
        args.functions,
        args.sparse_cpu_def_ops,
        args.sparse_cpu_attr_ops,
        args.ipex_cpu_ops_head,
        args.ipex_cpu_ops_cpp)
    sparse_code_gen.gen_code()
