#
# Copyright 2025 Hewlett Packard Enterprise Development LP
#
# SPDX-License-Identifier: Apache-2.0
import ast

import testtools

from bandit.core import name_resolution


def _call_pairs(source):
    """Return every call as a ``(lineno, resolved_qualname)`` pair."""
    tree = ast.parse(source)
    resolver = name_resolution.NameResolver(tree)
    return [
        (node.lineno, resolver.resolve_call(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]


class NameResolverTests(testtools.TestCase):
    def test_import_aliases_and_direct_assignment(self):
        source = """
import subprocess as sp
from subprocess import run as r

f = sp.run
f('ls', shell=True)
g = r
g('ls', shell=True)
"""
        self.assertEqual(
            [(6, "subprocess.run"), (8, "subprocess.run")],
            _call_pairs(source),
        )

    def test_multi_level_alias_chain(self):
        source = """
import subprocess as sp
f = sp.run
g = f
h = g
h('ls', shell=True)
"""
        self.assertEqual([(6, "subprocess.run")], _call_pairs(source))

    def test_tuple_and_list_assignment(self):
        source = """
import subprocess as sp
a, b = sp.run, sp.Popen
[c, d] = sp.call, sp.check_call
a('ls')
b('ls')
c('ls')
d('ls')
"""
        self.assertEqual(
            [
                (5, "subprocess.run"),
                (6, "subprocess.Popen"),
                (7, "subprocess.call"),
                (8, "subprocess.check_call"),
            ],
            _call_pairs(source),
        )

    def test_mismatched_tuple_is_unproven(self):
        source = """
import subprocess as sp
a, b = (sp.run,)
a('ls')
"""
        self.assertEqual([(4, None)], _call_pairs(source))

    def test_alias_through_module_alias(self):
        source = """
import subprocess as sp
mod = sp
f = mod.run
f('ls')
"""
        self.assertEqual([(5, "subprocess.run")], _call_pairs(source))

    def test_ordinary_function_shadows_imported_name(self):
        source = """
from subprocess import run

def run(cmd):
    return None
run('ls')
"""
        self.assertEqual([(6, None)], _call_pairs(source))

    def test_parameter_shadows_outer_alias(self):
        source = """
import subprocess as sp
f = sp.run

def wrapper(f):
    f('ls')
"""
        self.assertEqual([(6, None)], _call_pairs(source))

    def test_local_rebinding_cuts_alias(self):
        source = """
import subprocess as sp
f = sp.run

def rebind():
    f = lambda *a, **k: None
    f('ls')
"""
        self.assertEqual([(7, None)], _call_pairs(source))

    def test_whole_function_locality(self):
        # A name assigned anywhere in a function is local for the whole
        # function, so the earlier reference must not use the outer alias.
        source = """
import subprocess as sp
f = sp.run

def use_before_assign():
    f('ls')
    f = sp.run
"""
        self.assertEqual([(6, None)], _call_pairs(source))

    def test_closure_read_keeps_outer_binding(self):
        source = """
import subprocess as sp

def outer():
    f = sp.run

    def inner():
        f('ls')
"""
        self.assertEqual([(8, "subprocess.run")], _call_pairs(source))

    def test_closure_rebinding_shadows(self):
        source = """
import subprocess as sp

def outer():
    f = sp.run

    def inner():
        f = lambda *a, **k: None
        f('ls')
"""
        self.assertEqual([(9, None)], _call_pairs(source))

    def test_class_body_not_visible_in_method(self):
        source = """
import subprocess as sp

class Worker:
    f = sp.run

    def method(self):
        f('ls')
"""
        self.assertEqual([(8, None)], _call_pairs(source))

    def test_class_body_call_itself_is_resolved(self):
        source = """
import subprocess as sp

class Worker:
    f = sp.run
    f('ls')
"""
        self.assertEqual([(6, "subprocess.run")], _call_pairs(source))

    def test_del_removes_binding(self):
        source = """
import subprocess as sp
f = sp.run
del f
g = sp.run
g('ls')
"""
        self.assertEqual([(6, "subprocess.run")], _call_pairs(source))

    def test_later_reassignment_invalidates_alias(self):
        source = """
import subprocess as sp
f = sp.run
f = something_dynamic
f('ls')
"""
        self.assertEqual([(5, None)], _call_pairs(source))

    def test_import_shadowing(self):
        source = """
import subprocess as sp
f = sp.run
import os as f
f.system('ls')
"""
        self.assertEqual([(5, "os.system")], _call_pairs(source))

    def test_disagreeing_branches_are_unproven(self):
        source = """
import subprocess as sp
if cond:
    f = sp.run
else:
    f = lambda *a, **k: None
f('ls')
"""
        self.assertEqual([(7, None)], _call_pairs(source))

    def test_agreeing_branches_keep_binding(self):
        source = """
import subprocess as sp
if cond:
    f = sp.run
else:
    f = sp.run
f('ls')
"""
        self.assertEqual([(7, "subprocess.run")], _call_pairs(source))

    def test_loop_rebinding_is_uncertain(self):
        source = """
import subprocess as sp
for i in range(3):
    f = sp.run if i else other
    f('ls')
"""
        # ``range`` itself is unproven, and the rebound name is unproven.
        self.assertEqual([(3, None), (5, None)], _call_pairs(source))

    def test_loop_does_not_destroy_outer_alias(self):
        source = """
import subprocess as sp
f = sp.run
for i in range(3):
    other = i
f('ls')
"""
        self.assertEqual(
            [(4, None), (6, "subprocess.run")], _call_pairs(source)
        )

    def test_global_and_nonlocal_are_unproven(self):
        source = """
import subprocess as sp
G = sp.run

def uses_global():
    global G
    G('ls')

def outer():
    f = sp.run

    def inner():
        nonlocal f
        f('ls')
"""
        self.assertEqual([(7, None), (14, None)], _call_pairs(source))

    def test_call_result_is_not_a_proven_alias(self):
        source = """
import subprocess as sp
f = sp.run(['x'])
f('ls')
"""
        # The RHS call resolves, but its result cannot be used as an alias.
        self.assertEqual(
            [(3, "subprocess.run"), (4, None)], _call_pairs(source)
        )

    def test_attribute_and_subscript_writes_keep_name_binding(self):
        source = """
import subprocess as sp
f = sp.run
f.flag = 1
storage = {}
storage['k'] = f
f('ls')
"""
        self.assertEqual([(7, "subprocess.run")], _call_pairs(source))

    def test_lambda_scope_and_parameters(self):
        source = """
import subprocess as sp
f = sp.run
shadow = lambda f: f('ls')
reader = lambda: f('ls')
"""
        self.assertEqual(
            [(4, None), (5, "subprocess.run")], _call_pairs(source)
        )

    def test_comprehension_scope(self):
        source = """
import subprocess as sp
f = sp.run
vals = [f(x) for x in range(2)]
"""
        # The comprehension reads the outer alias for f; range is unproven.
        self.assertEqual(
            [(4, "subprocess.run"), (4, None)], _call_pairs(source)
        )

    def test_plain_import_keeps_root_name(self):
        # Mirrors the legacy import semantics: ``import urllib.request``
        # binds the root name, not the full dotted path.
        source = """
import urllib.request
urllib.request.urlopen('x')
"""
        self.assertEqual(
            [(3, "urllib.request.urlopen")], _call_pairs(source)
        )

    def test_nested_def_name_shadows(self):
        source = """
import subprocess as sp
f = sp.run

def f():
    pass
f('ls')
"""
        self.assertEqual([(7, None)], _call_pairs(source))

    def test_async_function_scope(self):
        source = """
import subprocess as sp

async def main():
    f = sp.run
    f('ls')
"""
        self.assertEqual([(6, "subprocess.run")], _call_pairs(source))
