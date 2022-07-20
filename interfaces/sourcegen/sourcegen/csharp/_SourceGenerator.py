# This file is part of Cantera. See License.txt in the top-level directory or
# at https://cantera.org/license.txt for license and copyright information.

from dataclasses import dataclass
import os
import re
import typing

from .._helpers import normalize, get_preamble
from .._types import *
from .._types import _unpack


@dataclass
class _CsFunc:
    """ Represents a C# method """

    ret_type: str
    name: str
    params: list[Param]
    del_clazz: typing.Optional[str]


    def __iter__(self):
        return _unpack(self)


class SourceGenerator(SourceGeneratorBase):
    _prolog = normalize("""
        [DllImport(LibFile)]
        public static extern
    """)

    _type_map = {
        'const char*': 'string',
        'const double*': 'double[]',
        'size_t': 'nuint',
        'char*': 'byte*'
    }

    _prop_type_map = {
        'byte*': 'string',
        'double*': 'double[]'
    }

    _preamble = '/*\n' + get_preamble() + '*/'

    @staticmethod
    def _join_params(params):
        return ', '.join((p.p_type + ' ' + p.name for p in params))


    @classmethod
    def _get_function_text(cls, function):
        ref_type, name, params, _ = function
        is_unsafe = any((p.p_type.endswith('*') for p in params))
        params_text = cls._join_params(params)
        if is_unsafe:
            return f'{cls._prolog} unsafe {ref_type} {name}({params_text});'
        else:
            return f'{cls._prolog} {ref_type} {name}({params_text});'


    @staticmethod
    def _get_base_handle_text(handle):
        name, del_clazz = handle

        handle = normalize(f'''
            class {del_clazz} : CanteraHandle
            {{
                protected override bool ReleaseHandle() =>
                    LibCantera.{name}(Value) == InteropConsts.Success;
            }}
        ''')

        return handle


    @staticmethod
    def _get_derived_handle_text(derived):
        derived, base = derived

        derived = f'''class {derived} : {base} {{ }}'''

        return derived


    def __init__(self, out_dir: str, config: dict):
        self._out_dir = out_dir
        self._config = config
        # as different clib files are passed in, the known funcs are added to this dict
        self._known_funcs = {}

        os.makedirs(out_dir, exist_ok=True)


    def _convert_func(self, parsed: Func):
        ret_type, name, params = parsed
        clazz, method = name.split('_', 1)

        #copy the params list
        params = list(params)

        del_clazz = None

        if clazz != 'ct':
            handle_clazz = self._config['handle_crosswalk'][clazz] + 'Handle'

            # It’s not a “global” function, therefore:
            #   * It wraps a constructor and returns a handle, or
            #   * It wraps an instance method and takes the handle as the first param.
            if method.startswith('del'):
                del_clazz = handle_clazz
            elif method.startswith('new'):
                ret_type = handle_clazz
            else:
                _, param_name = params[0]
                params[0] = handle_clazz, param_name

        for c_type, cs_type in self._type_map.items():
            if ret_type == c_type:
                ret_type = cs_type
                break

        setter_double_arrays_count = 0

        for i in range(0, len(params)):
            param_type, param_name = params[i]

            for c_type, cs_type in self._type_map.items():
                if param_type == c_type:
                    param_type = cs_type
                    break

            # Most "setter" functions for arrays in CLib use a const double*,
            # but we also need to handle the cases for a plain double*
            if param_type == 'double*' and method.startswith('set'):
                setter_double_arrays_count += 1
                if setter_double_arrays_count > 1:
                    # We assume a double* can reliably become a double[].
                    # However, this logic is too simplistic if there is
                    # more than one array.
                    raise ValueError(f'Cannot scaffold {name} with '
                        + 'more than one array of doubles!')

                if clazz == 'thermo' and re.match('^set_[A-Z]{2}$', method):
                    # Special case for the functions that set thermo pairs
                    # This allows the C# side to pass a pointer to the stack
                    # Rather than allocating an array on the heap (which requires GC)
                    param_type = '(double, double)*'
                else:
                    param_type = 'double[]'

            params[i] = Param(param_type, param_name)

        func = _CsFunc(ret_type, name, params, del_clazz)
        self._known_funcs[name] = func

        return func


    def _get_property_text(self, clazz: str, c_name: str, cs_name: str):
        getter = self._known_funcs.get(clazz + "_" + c_name)

        if (getter):
            # here we have found a simple scalar property
            prop_type = getter.ret_type
        else:
            # here we have found an array-like property (string, double[])
            getter = self._known_funcs[clazz + "_get" + c_name.capitalize()]
            # this assumes the last param in the function is a pointer type,
            # from which we determine the appropriate C# type
            prop_type = self._prop_type_map[getter.params[-1].p_type]

        setter = self._known_funcs.get(clazz + "_set" + c_name.capitalize())

        if prop_type in ['int', 'double']:
            text = f'''
                public {prop_type} {cs_name}
                {{
                    get => InteropUtil.CheckReturn(
                        LibCantera.{getter.name}(_handle));'''

            if(setter):
                text += f'''
                    set => InteropUtil.CheckReturn(
                        LibCantera.{setter.name}(_handle, value));'''

            text += '''
                }
            '''
        elif (prop_type == 'string'):
            p_type = getter.params[1].p_type

            # for get-string type functions we need to look up the type of the second
            # (index 1) param for a cast because sometimes it's an int and other times
            # its a nuint (size_t)
            text = f'''
                public unsafe string {cs_name}
                {{
                    get => InteropUtil.GetString(40, (length, buffer) =>
                        LibCantera.{getter.name}(_handle, ({p_type}) length, buffer));
            '''

            if(setter):
                text += f'''
                    set => InteropUtil.CheckReturn(
                        LibCantera.{setter.name}(_handle, value));'''

            text += '''
                }
            '''
        else:
            raise ValueError(f'Unable to scaffold properties of type {prop_type}!')

        return(normalize(text))


    def generate_source(self, incl_file: os.DirEntry, funcs: list[Func]):
        cs_funcs = [self._convert_func(f) for f in funcs]

        functions_text = '\n\n'.join((self._get_function_text(f) for f in cs_funcs))

        interop_text = normalize(f'''
            {normalize(self._preamble, 12)}

            using System.Runtime.InteropServices;

            namespace Cantera.Interop;

            static partial class LibCantera
            {{
                {normalize(functions_text, 16, True)}
            }}
        ''')

        with open(self._out_dir + 'Interop.LibCantera.'
                + incl_file.name + '.g.cs', 'w') as f:
            f.write(interop_text)

        handles = [(name, del_clazz) for _, name, _, del_clazz in cs_funcs if del_clazz]

        if not handles:
            return

        handles_text = '\n\n'.join((self._get_base_handle_text(h) for h in handles))

        handles_text = normalize(f'''
            {normalize(self._preamble, 12)}

            namespace Cantera.Interop;

            {normalize(handles_text, 12, True)}
        ''')

        with open(self._out_dir + 'Interop.Handles.'
                + incl_file.name + '.g.cs', 'w') as f:
            f.write(handles_text)


    def finalize(self):
        derived_handles = '\n\n'.join((self._get_derived_handle_text(d)
            for d in self._config['derived_handles'].items()))

        derived_handles_text = normalize(f'''
            {normalize(self._preamble, 12)}

            namespace Cantera.Interop;

            {derived_handles}
        ''')

        with open(self._out_dir + 'Interop.Handles.g.cs', 'w') as f:
            f.write(derived_handles_text)

        for (clazz, props) in self._config['classes'].items():
            name = self._config['handle_crosswalk'][clazz]

            props_text = '\n\n'.join((self._get_property_text(clazz, c_name, cs_name)
                for (c_name, cs_name) in props.items()))

            clazz_text = normalize(f'''
                {normalize(self._preamble, 16)}

                using Cantera.Interop;

                namespace Cantera;

                public partial class {name} : IDisposable
                {{
                    readonly {name}Handle _handle;

                    {normalize(props_text, 20, True)}

                    public void Dispose() =>
                        _handle.Dispose();
                }}
            ''')

            with open(self._out_dir + name + '.g.cs', 'w') as f:
                f.write(clazz_text)
