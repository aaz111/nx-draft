"""找出提取模块里真正的闭包依赖：函数体中读取、但既不是参数也不是局部变量的名字。

静态扫一遍，用来确认需要注入哪些运行时对象，以及哪些辅助函数忘了带过来。
"""

import ast
import io
import sys


def function_deps(node):
    """一个函数体内引用的自由名字（去掉参数、局部赋值、嵌套函数名）。"""
    local = set()
    for argument in node.args.args:
        local.add(argument.arg)
    if node.args.vararg:
        local.add(node.args.vararg.arg)
    if node.args.kwarg:
        local.add(node.args.kwarg.arg)
    for argument in node.args.kwonlyargs:
        local.add(argument.arg)

    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, (ast.Store, ast.Del)):
            local.add(inner.id)
        elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(inner.name)
        elif isinstance(inner, ast.ExceptHandler) and inner.name:
            local.add(inner.name)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            for alias in inner.names:
                local.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(inner, ast.arg):
            local.add(inner.arg)

    free = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
            if inner.id not in local:
                free.add(inner.id)
    return free


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "nxdraft/_extracted.py"
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source)

    module_level = set()
    functions = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            module_level.add(node.name)
            functions.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    module_level.add(target.id)

    import builtins
    known = module_level | set(dir(builtins))
    all_free = {}
    for node in functions:
        free = function_deps(node)
        external = free - known
        if external:
            all_free[node.name] = sorted(external)

    if not all_free:
        print("没有未解析的自由名字")
        return
    for name in sorted(all_free):
        print("%s: %s" % (name, ", ".join(all_free[name])))


if __name__ == "__main__":
    main()
