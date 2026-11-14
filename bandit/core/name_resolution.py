#
# Copyright 2025 Hewlett Packard Enterprise Development LP
#
# SPDX-License-Identifier: Apache-2.0
"""Static, intra-file name resolution for simple assignments.

Bandit historically only understood the aliasing created by import
statements (``import subprocess as sp``,
``from subprocess import run as r``).  A plain assignment such as
``f = sp.run`` was invisible to it, so a later ``f(cmd, shell=True)``
could not be tied back to ``subprocess.run``.

``NameResolver`` walks a parsed module once, tracks imports and simple
assignments through lexical scopes, and records the qualified name of
every call whose callee can be *proven* statically.  The result is a
simple ``ast.Call`` -> qualified name mapping that other parts of
bandit can query while doing their own AST traversal.

Only relationships that can be proven are recorded, for example:

* simple assignment ``a = <name or attribute access>``
* chaining / re-aliasing ``a = b`` where ``b`` is itself an alias
* tuple/list assignment ``a, b = sp.run, sp.Popen``
* nested functions, methods, classes, lambdas and comprehensions

Anything dynamic (calls on the right hand side, subscripts, attribute
*writes*, control flow that may rebind a name, ``del``, parameters,
``global``/``nonlocal``, import shadowing) either invalidates the
affected binding or is left unresolved.  The analysis never executes
the scanned code and never leaves the file.
"""
import ast


_MODULE = "module"
_CLASS = "class"
_FUNCTION = "function"

# match/case pattern nodes only exist on Python 3.10+.
_MATCH = getattr(ast, "Match", ())
_MATCH_AS = getattr(ast, "MatchAs", ())
_MATCH_STAR = getattr(ast, "MatchStar", ())
_MATCH_MAPPING = getattr(ast, "MatchMapping", ())


class _Scope:
    """A lexical scope.

    ``bindings`` maps a local name to either a proven qualified name
    string or ``None``.  A name present with value ``None`` is known to
    be local but unproven, and therefore shadows anything an enclosing
    scope would provide.
    """

    def __init__(self, kind):
        self.kind = kind
        self.bindings = {}
        self.parent = None
        # Names declared global/nonlocal cannot be proven through the
        # normal lexical chain inside this scope.
        self.global_names = set()
        self.nonlocal_names = set()

    def push(self, kind):
        child = _Scope(kind)
        child.parent = self
        return child


class NameResolver:
    """Resolves call expressions to proven qualified names for a file."""

    def __init__(self, tree):
        self.tree = tree
        # Only proven (non-None) resolutions are stored here.
        self.call_qualnames = {}
        self.root = _Scope(_MODULE)
        self._stack = [self.root]
        self._walk_body(tree.body, self.root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def resolve_call(self, call_node):
        """Return the proven qualified name of a call, or ``None``."""
        return self.call_qualnames.get(call_node)

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------
    @property
    def _scope(self):
        return self._stack[-1]

    def _push(self, kind):
        self._stack.append(self._scope.push(kind))
        return self._stack[-1]

    def _pop(self):
        self._stack.pop()

    def _snapshot(self):
        return dict(self._scope.bindings)

    def _restore(self, snapshot):
        self._scope.bindings = snapshot

    def _invalidate(self, name):
        self._scope.bindings[name] = None

    def _lookup(self, name):
        """Lexical name lookup following Python's scoping rules."""
        start = self._scope
        scope = start
        while scope is not None:
            # Class scope names are only visible from the class body
            # itself, never from methods or other nested scopes.
            if scope.kind == _CLASS and scope is not start:
                scope = scope.parent
                continue
            if name in scope.global_names or name in scope.nonlocal_names:
                # Declared to live elsewhere; treat as unproven.
                return None
            if name in scope.bindings:
                # An explicit (even unproven) local binding shadows
                # everything an enclosing scope has to offer.
                return scope.bindings[name]
            scope = scope.parent
        return None

    def _resolve_expr(self, node):
        """Qualified name of a name/attribute expression, else None."""
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve_expr(node.value)
            if base is None:
                return None
            return f"{base}.{node.attr}"
        return None

    # ------------------------------------------------------------------
    # Statement walking
    # ------------------------------------------------------------------
    def _walk_body(self, statements, scope):
        for stmt in statements:
            self._stmt(stmt, scope)

    def _stmt(self, stmt, scope):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # The def itself binds a local name of unknown value.
            self._invalidate(stmt.name)
            # Defaults, decorators and annotations are evaluated in the
            # enclosing scope.
            for default in stmt.args.defaults + stmt.args.kw_defaults:
                if default is not None:
                    self._expr(default, scope)
            for decorator in stmt.decorator_list:
                self._expr(decorator, scope)
            if stmt.returns is not None:
                self._expr(stmt.returns, scope)
            for annotation in self._iter_annotations(stmt):
                self._expr(annotation, scope)

            func_scope = self._push(_FUNCTION)
            self._seed_function_locals(stmt, func_scope)
            self._walk_body(stmt.body, func_scope)
            self._pop()
            return

        if isinstance(stmt, ast.ClassDef):
            self._invalidate(stmt.name)
            for base in stmt.bases:
                self._expr(base, scope)
            for keyword in stmt.keywords:
                self._expr(keyword.value, scope)
            for decorator in stmt.decorator_list:
                self._expr(decorator, scope)
            class_scope = self._push(_CLASS)
            self._walk_body(stmt.body, class_scope)
            self._pop()
            return

        if isinstance(stmt, ast.Global):
            scope.global_names.update(stmt.names)
            for name in stmt.names:
                # Any earlier local guess is now unreliable.
                scope.bindings[name] = None
            self._scan_inner(stmt, scope)
            return

        if isinstance(stmt, ast.Nonlocal):
            scope.nonlocal_names.update(stmt.names)
            for name in stmt.names:
                scope.bindings[name] = None
            self._scan_inner(stmt, scope)
            return

        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self._invalidate(target.id)
            self._scan_inner(stmt, scope)
            return

        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    # ``import a.b as c``: the alias stands for the full
                    # dotted module name.
                    self._bind(alias.asname, alias.name)
                else:
                    # Plain ``import a.b`` only binds the root name, so
                    # attribute chains such as ``a.b.fn`` resolve naturally.
                    root = alias.name.split(".")[0]
                    self._bind(root, root)
            return

        if isinstance(stmt, ast.ImportFrom):
            module = stmt.module
            if module is None:
                # Relative ``from . import x``: mirrors ast.Import rules.
                for alias in stmt.names:
                    if alias.name == "*":
                        continue
                    if alias.asname:
                        self._bind(alias.asname, alias.name)
                    else:
                        self._bind(alias.name, alias.name)
            else:
                for alias in stmt.names:
                    if alias.name == "*":
                        # Star imports cannot be tracked.
                        continue
                    bound = alias.asname or alias.name
                    self._bind(bound, f"{module}.{alias.name}")
            return

        if isinstance(stmt, ast.Assign):
            self._expr(stmt.value, scope)
            self._assign(stmt.targets, stmt.value, scope)
            return

        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self._expr(stmt.value, scope)
                self._assign([stmt.target], stmt.value, scope)
            return

        if isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                self._invalidate(stmt.target.id)
            self._scan_inner(stmt, scope)
            return

        if isinstance(stmt, ast.If):
            self._expr(stmt.test, scope)
            snapshot = self._snapshot()
            self._walk_body(stmt.body, scope)
            after_body = self._snapshot()
            self._restore(snapshot)
            self._walk_body(stmt.orelse, scope)
            after_else = self._snapshot()
            self._merge_branches(snapshot, after_body, after_else)
            return

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._expr(stmt.iter, scope)
            snapshot = self._snapshot()
            self._bind_target(stmt.target, None, scope)
            # A loop can repeat, so anything rebound in the body cannot
            # be trusted at the body's entry on later iterations.
            for name in self._bound_names_under_list(
                stmt.body + stmt.orelse
            ):
                self._invalidate(name)
            self._walk_body(stmt.body, scope)
            self._walk_body(stmt.orelse, scope)
            self._merge_uncertain(snapshot)
            return

        if isinstance(stmt, ast.While):
            self._expr(stmt.test, scope)
            snapshot = self._snapshot()
            for name in self._bound_names_under_list(
                stmt.body + stmt.orelse
            ):
                self._invalidate(name)
            self._walk_body(stmt.body, scope)
            self._walk_body(stmt.orelse, scope)
            self._merge_uncertain(snapshot)
            return

        if isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ()))):
            snapshot = self._snapshot()
            self._walk_body(stmt.body, scope)
            for handler in stmt.handlers:
                if handler.name is not None:
                    self._invalidate(handler.name)
                self._walk_body(handler.body, scope)
            self._walk_body(stmt.orelse, scope)
            self._walk_body(stmt.finalbody, scope)
            self._merge_uncertain(snapshot)
            return

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self._expr(item.context_expr, scope)
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, None, scope)
            self._walk_body(stmt.body, scope)
            return

        if _MATCH and isinstance(stmt, _MATCH):
            self._expr(stmt.subject, scope)
            snapshot = self._snapshot()
            for case in stmt.cases:
                # Pattern capture names only bind when the case matches,
                # so treat them as unproven rather than aliases.
                for name in self._names_in_pattern(case.pattern):
                    self._invalidate(name)
                if case.guard is not None:
                    self._expr(case.guard, scope)
                self._walk_body(case.body, scope)
            self._merge_uncertain(snapshot)
            return

        # Return, raise, expr statements, etc.
        self._scan_inner(stmt, scope)

    def _scan_inner(self, node, scope):
        """Walk everything inside a statement without special handling."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._stmt(child, scope)
            else:
                self._expr(child, scope)

    def _merge_branches(self, snapshot, after_body, after_else):
        """Keep a post-if binding only if both branches prove the same."""
        changed = set(after_body) | set(after_else)
        merged = dict(snapshot)
        for name in changed:
            if after_body.get(name) == after_else.get(name):
                merged[name] = after_body[name]
            else:
                merged[name] = None
        self._scope.bindings = merged

    def _merge_uncertain(self, snapshot):
        """Invalidate every name a block might have rebound."""
        current = self._scope.bindings
        for name in set(current) | set(snapshot):
            if current.get(name) != snapshot.get(name):
                current[name] = None

    # ------------------------------------------------------------------
    # Assignment handling
    # ------------------------------------------------------------------
    def _bind(self, name, qualname):
        scope = self._scope
        if name in scope.global_names or name in scope.nonlocal_names:
            scope.bindings[name] = None
            return
        scope.bindings[name] = qualname

    def _assign(self, targets, value_node, scope):
        if len(targets) == 1 and isinstance(targets[0], (ast.Tuple, ast.List)):
            target_elts = targets[0].elts
            if isinstance(value_node, (ast.Tuple, ast.List)) and len(
                value_node.elts
            ) == len(target_elts):
                for target, value in zip(target_elts, value_node.elts):
                    self._bind_target(target, value, scope)
                return
            for target in target_elts:
                self._bind_target(target, None, scope)
            return

        for target in targets:
            self._bind_target(target, value_node, scope)

    def _bind_target(self, target, value_node, scope):
        if isinstance(target, ast.Name):
            if value_node is not None:
                qualname = self._resolve_expr(value_node)
            else:
                qualname = None
            self._bind(target.id, qualname)
        elif isinstance(target, (ast.Tuple, ast.List)):
            # Nested unpacking without a matching iterable cannot be
            # proven positionally.
            for elt in target.elts:
                self._bind_target(elt, None, scope)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value, None, scope)
        # Attribute and subscript writes (a.b = ..., a[0] = ...) cannot
        # be tracked and do not create name bindings.

    # ------------------------------------------------------------------
    # Expression walking (call discovery, lambdas, comprehensions)
    # ------------------------------------------------------------------
    def _expr(self, node, scope):
        if node is None:
            return
        if isinstance(node, ast.Lambda):
            inner = self._push(_FUNCTION)
            self._mark_parameters(node.args, inner)
            self._expr(node.body, inner)
            self._pop()
            return
        if isinstance(
            node,
            (
                ast.ListComp,
                ast.SetComp,
                ast.GeneratorExp,
                ast.DictComp,
            ),
        ):
            inner = self._push(_FUNCTION)
            for generator in node.generators:
                self._bind_target(generator.target, None, inner)
                self._expr(generator.iter, inner)
                for condition in generator.ifs:
                    self._expr(condition, inner)
            if isinstance(node, ast.DictComp):
                self._expr(node.key, inner)
                self._expr(node.value, inner)
            else:
                self._expr(node.elt, inner)
            self._pop()
            return
        if isinstance(node, ast.NamedExpr):
            # Walrus scope rules are subtle; stay conservative.
            if isinstance(node.target, ast.Name):
                self._invalidate(node.target.id)
            self._expr(node.value, scope)
            return
        if isinstance(node, ast.Call):
            qualname = self._resolve_expr(node.func)
            if qualname is not None:
                self.call_qualnames[node] = qualname

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._stmt(child, scope)
            else:
                self._expr(child, scope)

    # ------------------------------------------------------------------
    # Function scope locality
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_annotations(func_node):
        args = func_node.args
        annotated = (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
        )
        for arg in annotated:
            if arg.annotation is not None:
                yield arg.annotation
        if args.vararg is not None and args.vararg.annotation is not None:
            yield args.vararg.annotation
        if args.kwarg is not None and args.kwarg.annotation is not None:
            yield args.kwarg.annotation

    @staticmethod
    def _mark_parameters(arguments, scope):
        params = (
            list(arguments.posonlyargs)
            + list(arguments.args)
            + list(arguments.kwonlyargs)
        )
        for arg in params:
            scope.bindings[arg.arg] = None
        if arguments.vararg is not None:
            scope.bindings[arguments.vararg.arg] = None
        if arguments.kwarg is not None:
            scope.bindings[arguments.kwarg.arg] = None

    def _seed_function_locals(self, func_node, func_scope):
        """Pre-mark every name bound anywhere in the function as local.

        Python treats a name assigned anywhere in a function body as a
        local for the *entire* function, so a reference above the
        assignment must not be resolved against an enclosing scope.
        Names declared global/nonlocal are excluded; nonlocal names are
        separately marked unproven.
        """
        self._mark_parameters(func_node.args, func_scope)
        global_names, nonlocal_names, bound = self._collect_bound_names(
            func_node
        )
        func_scope.global_names.update(global_names)
        func_scope.nonlocal_names.update(nonlocal_names)
        for name in bound:
            func_scope.bindings.setdefault(name, None)

    def _collect_bound_names(self, func_node):
        """Collect names bound directly within ``func_node``'s scope.

        Nested function and class bodies are skipped because their
        bindings (and their own global/nonlocal declarations) belong to
        their own scopes.
        """
        global_names = set()
        nonlocal_names = set()

        stack = list(ast.iter_child_nodes(func_node))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The def name binds here, but the body is another scope.
                continue
            if isinstance(node, ast.ClassDef):
                continue
            if isinstance(node, ast.Global):
                global_names.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                nonlocal_names.update(node.names)
            stack.extend(ast.iter_child_nodes(node))

        bound = self._bound_names_under_list(func_node.body)
        # global names are not locals; nonlocal names resolve elsewhere.
        bound -= global_names
        bound -= nonlocal_names
        return global_names, nonlocal_names, bound

    def _bound_names_under_list(self, statements):
        """Names bound directly within these statements.

        Nested function and class bodies are excluded because their
        bindings belong to their own scopes.
        """
        names = set()
        for stmt in statements:
            names.update(self._bound_names_under(stmt))
        return names

    def _bound_names_under(self, node):
        names = self._names_bound_by_node(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(child.name)
                continue
            if isinstance(child, ast.ClassDef):
                names.add(child.name)
                continue
            names.update(self._names_bound_by_node(child))
            names.update(self._bound_names_under(child))
        return names

    def _names_bound_by_node(self, node):
        """Names bound directly by a single node (not its descendants)."""
        names = set()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(self._names_in_target(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(self._names_in_target(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                names.update(self._names_in_target(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif _MATCH_AS and isinstance(node, _MATCH_AS) and node.name:
            names.add(node.name)
        elif _MATCH_STAR and isinstance(node, _MATCH_STAR) and node.name:
            names.add(node.name)
        elif _MATCH_MAPPING and isinstance(
            node, _MATCH_MAPPING
        ) and node.rest:
            names.add(node.rest)
        return names

    def _names_in_pattern(self, pattern):
        """Capture names introduced by a match/case pattern."""
        names = set()
        if not _MATCH_AS or pattern is None:
            return names
        for node in ast.walk(pattern):
            if _MATCH_AS and isinstance(node, _MATCH_AS) and node.name:
                names.add(node.name)
            elif _MATCH_STAR and isinstance(node, _MATCH_STAR) and node.name:
                names.add(node.name)
            elif _MATCH_MAPPING and isinstance(
                node, _MATCH_MAPPING
            ) and node.rest:
                names.add(node.rest)
        return names

    @staticmethod
    def _names_in_target(target):
        if target is None:
            return set()
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            names = set()
            for elt in target.elts:
                names.update(NameResolver._names_in_target(elt))
            return names
        if isinstance(target, ast.Starred):
            return NameResolver._names_in_target(target.value)
        return set()
