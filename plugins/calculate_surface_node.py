bl_info = {
    "name": "Calculate Surface",
    "author": "Alessio Fumagalli",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "Geometry Nodes > Add",
    "description": "Calculate surface from parametric curve and transformation matrix",
    "category": "Node",
}

import bpy
import bmesh
from mathutils import Vector, Matrix
from math import pi, e, tau
import re
import random  # <<< ADDED (only for hidden random)

GROUP_NAME = "Calculate Surface"

# --- Hidden random (ADDED) ---
DEFAULT_DIGITS = 100
HIDDEN_PROP = "hidden_random"
DIGITS_PROP = "hidden_random_digits"
MIN_PROP = "hidden_random_min"
MAX_PROP = "hidden_random_max"


def rand_with_digits(digits: int) -> int:
    if not isinstance(digits, int) or digits < 1:
        raise ValueError("digits must be an integer >= 1")
    lo = 10 ** (digits - 1)
    hi = 10 ** digits - 1
    return random.randint(lo, hi)


# ==================== Expression Parser ====================
_TOKEN_SPEC = [
    ("NUMBER", r"\d+(\.\d+)?"),
    ("ID", r"[A-Za-z_]\w*"),
    ("OP", r"[\+\-\*/\^\(\),]"),
    ("SKIP", r"[ \t]+"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))

_PRECEDENCE = {
    "^": (4, "right"),
    "*": (3, "left"),
    "/": (3, "left"),
    "+": (2, "left"),
    "-": (2, "left"),
}

_FUNC_1 = {
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "sqrt",
    "abs",
    "exp",
    "ln",
    "floor",
    "ceil",
    "frac",
}
_FUNC_2 = {"pow", "log", "min", "max", "mod"}


def tokenize(expr):
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"Unexpected character at {pos}: '{expr[pos]}'")
        typ = m.lastgroup
        txt = m.group()
        pos = m.end()
        if typ == "SKIP":
            continue
        yield (typ, txt)


def to_rpn(expr):
    expr = expr.strip()
    if not expr:
        raise ValueError("Expression is empty")

    out = []
    stack = []
    arg_count = []

    prev_token = None
    for typ, txt in tokenize(expr):
        if typ == "NUMBER":
            out.append(("NUMBER", txt))
        elif typ == "ID":
            out.append(("ID", txt))
        elif typ == "OP":
            if txt == "(":
                if prev_token and prev_token[0] == "ID":
                    stack.append(("FUNC", prev_token[1]))
                    out.pop()
                    arg_count.append(1)
                stack.append(("OP", "("))
            elif txt == ",":
                while stack and not (stack[-1][0] == "OP" and stack[-1][1] == "("):
                    out.append(stack.pop())
                if arg_count:
                    arg_count[-1] += 1
            elif txt == ")":
                while stack and not (stack[-1][0] == "OP" and stack[-1][1] == "("):
                    out.append(stack.pop())
                if stack:
                    stack.pop()
                if stack and stack[-1][0] == "FUNC":
                    func = stack.pop()[1]
                    nargs = arg_count.pop() if arg_count else 1
                    out.append(("FUNC", func, nargs))
            else:
                is_unary = prev_token is None or (
                    prev_token[0] == "OP"
                    and prev_token[1] in ("(", "+", "-", "*", "/", "^", ",")
                )
                if is_unary and txt in ("-", "+"):
                    if txt == "-":
                        stack.append(("UNARY", "neg"))
                else:
                    while stack:
                        top = stack[-1]
                        if top[0] == "OP":
                            top_prec, top_assoc = _PRECEDENCE.get(top[1], (0, "left"))
                            txt_prec, txt_assoc = _PRECEDENCE.get(txt, (0, "left"))
                            if (txt_assoc == "left" and top_prec >= txt_prec) or (
                                txt_assoc == "right" and top_prec > txt_prec
                            ):
                                out.append(stack.pop())
                            else:
                                break
                        else:
                            break
                    stack.append(("OP", txt))
        prev_token = (typ, txt)

    while stack:
        out.append(stack.pop())
    return out


def eval_rpn(rpn, variables):
    """Evaluate RPN expression"""
    import math

    stack = []
    for tok in rpn:
        if tok[0] == "NUMBER":
            stack.append(float(tok[1]))
        elif tok[0] == "ID":
            var_name = tok[1].lower()
            if var_name in variables:
                stack.append(variables[var_name])
            elif var_name == "pi":
                stack.append(pi)
            elif var_name == "e":
                stack.append(e)
            elif var_name == "tau":
                stack.append(tau)
            else:
                raise ValueError(f"Unknown variable: {var_name}")
        elif tok[0] == "OP":
            op = tok[1]
            b = stack.pop()
            a = stack.pop()
            if op == "+":
                stack.append(a + b)
            elif op == "-":
                stack.append(a - b)
            elif op == "*":
                stack.append(a * b)
            elif op == "/":
                stack.append(a / b if b != 0 else 0)
            elif op == "^":
                stack.append(a**b)
        elif tok[0] == "UNARY":
            stack.append(-stack.pop())
        elif tok[0] == "FUNC":
            fname = tok[1].lower()
            nargs = tok[2] if len(tok) > 2 else 1
            if nargs == 1:
                arg = stack.pop()
                if fname == "sin":
                    stack.append(math.sin(arg))
                elif fname == "cos":
                    stack.append(math.cos(arg))
                elif fname == "tan":
                    stack.append(math.tan(arg))
                elif fname == "asin":
                    stack.append(math.asin(arg))
                elif fname == "acos":
                    stack.append(math.acos(arg))
                elif fname == "atan":
                    stack.append(math.atan(arg))
                elif fname == "sqrt":
                    stack.append(math.sqrt(arg))
                elif fname == "abs":
                    stack.append(abs(arg))
                elif fname == "exp":
                    stack.append(math.exp(arg))
                elif fname == "ln":
                    stack.append(math.log(arg))
                elif fname == "floor":
                    stack.append(math.floor(arg))
                elif fname == "ceil":
                    stack.append(math.ceil(arg))
                elif fname == "frac":
                    stack.append(arg - math.floor(arg))
            elif nargs == 2:
                b = stack.pop()
                a = stack.pop()
                if fname == "pow":
                    stack.append(a**b)
                elif fname == "log":
                    stack.append(math.log(a, b))
                elif fname == "min":
                    stack.append(min(a, b))
                elif fname == "max":
                    stack.append(max(a, b))
                elif fname == "mod":
                    stack.append(a % b)
    return stack[0] if stack else 0


def ensure_socket(iface, name, in_out, socket_type, default=None):
    sock = next(
        (
            s
            for s in iface.items_tree
            if s.name == name and getattr(s, "in_out", None) == in_out
        ),
        None,
    )
    if not sock:
        sock = iface.new_socket(name, socket_type=socket_type, in_out=in_out)
    if default is not None and hasattr(sock, "default_value"):
        sock.default_value = default
    return sock




def _find_curve_source_node(tree, calc_node):
    if "Curve Geometry" not in calc_node.inputs:
        return None
    for link in tree.links:
        if link.to_socket == calc_node.inputs["Curve Geometry"]:
            return link.from_node
    return None


def _find_matrix_node(tree, calc_node):
    if "Matrix" not in calc_node.inputs:
        return None
    for link in tree.links:
        if link.to_socket == calc_node.inputs["Matrix"]:
            source_node = link.from_node
            if (
                source_node.bl_idname == "GeometryNodeGroup"
                and source_node.node_tree
                and source_node.node_tree.name.startswith("Parametric Transformation Matrix")
            ):
                return source_node
    return None


def _is_parametric_curve_node(node):
    return bool(
        node
        and node.bl_idname == "GeometryNodeGroup"
        and node.node_tree
        and node.node_tree.name.startswith("Parametric Curve")
    )


def _is_bezier_curve_node(node):
    return bool(
        node
        and node.bl_idname == "GeometryNodeGroup"
        and node.node_tree
        and node.node_tree.name.startswith("Bezier Curve")
    )


def _find_socket_by_names(sockets, names):
    for name in names:
        sock = sockets.get(name)
        if sock is not None:
            return sock
    for sock in sockets:
        if sock.name in names:
            return sock
    return sockets[0] if len(sockets) else None


def _find_input_source_node(tree, node, input_name):
    if input_name not in node.inputs:
        return None
    target = node.inputs[input_name]
    for link in tree.links:
        if link.to_node == node and link.to_socket == target:
            return link.from_node
    return None


def _node_group_name(node):
    return (
        node.node_tree.name
        if node
        and getattr(node, "bl_idname", None) == "GeometryNodeGroup"
        and getattr(node, "node_tree", None)
        else ""
    )


def _is_bezier_connection_handles_node(node):
    return _node_group_name(node).startswith("Bezier Connection Handles")


def _socket_default_bool(node, socket_name, default=False):
    sock = node.inputs.get(socket_name) if hasattr(node, "inputs") else None
    if sock is None or not hasattr(sock, "default_value"):
        return default
    try:
        return bool(sock.default_value)
    except Exception:
        return default


def _socket_default_float(node, socket_name, default=1.0):
    sock = node.inputs.get(socket_name) if hasattr(node, "inputs") else None
    if sock is None or not hasattr(sock, "default_value"):
        return default
    try:
        return float(sock.default_value)
    except Exception:
        return default


def _extract_bezier_connection_handle_point(tree, node, _depth=0):
    """Resolve Bezier Connection Handles/B Second analytically.

    The node outputs one point:
        B Second = A End + G1 * (n_in / n_out) * (A End - A Prev)

    where n_in/n_out are 2 for quadratic and 3 for cubic.
    """
    if _depth > 20:
        raise RuntimeError("Too many nested Bezier point connections")

    a_end_node = _find_input_source_node(tree, node, "A End")
    a_prev_node = _find_input_source_node(tree, node, "A Prev")

    a_end = _vector_from_source_node(tree, a_end_node, _depth=_depth + 1)
    a_prev = _vector_from_source_node(tree, a_prev_node, _depth=_depth + 1)

    n_in = 3.0 if _socket_default_bool(node, "Input Is Cubic (Deg3)", False) else 2.0
    n_out = 3.0 if _socket_default_bool(node, "Output Is Cubic (Deg3)", False) else 2.0
    g1 = _socket_default_float(node, "G1", 1.0)

    return a_end + (g1 * (n_in / n_out)) * (a_end - a_prev)


def _vector_from_source_node(tree, node, socket_name_hint="Position", _depth=0):
    if node is None:
        raise RuntimeError("Missing Bezier input connection")
    if _depth > 20:
        raise RuntimeError("Too many nested Bezier point connections")

    # NEW: support points generated by the custom Bezier Connection Handles node.
    # That node does not expose a static Position default_value; it outputs a
    # one-point geometry computed from A End, A Prev, G1 and degree toggles.
    if _is_bezier_connection_handles_node(node):
        return _extract_bezier_connection_handle_point(tree, node, _depth=_depth + 1)

    if hasattr(node, "inputs"):
        sock = node.inputs.get(socket_name_hint)
        if sock is not None and hasattr(sock, "default_value"):
            dv = sock.default_value
            try:
                return Vector((float(dv[0]), float(dv[1]), float(dv[2])))
            except Exception:
                pass

        # Fallback: any 3D vector input with a default value.
        for sock in node.inputs:
            if hasattr(sock, "default_value"):
                dv = sock.default_value
                try:
                    if len(dv) >= 3:
                        return Vector((float(dv[0]), float(dv[1]), float(dv[2])))
                except Exception:
                    pass

    raise RuntimeError(
        f"Unsupported Bezier control input source: {getattr(node, 'bl_idname', type(node).__name__)}. "
        "Use primitive Points nodes, nodes exposing a Position vector input, "
        "or the Bezier Connection Handles node."
    )


def _extract_bezier_controls(tree, bezier_node):
    p0 = _vector_from_source_node(tree, _find_input_source_node(tree, bezier_node, "P0"))
    p1 = _vector_from_source_node(tree, _find_input_source_node(tree, bezier_node, "P1"))
    p2_node = _find_input_source_node(tree, bezier_node, "P2")
    p3_node = _find_input_source_node(tree, bezier_node, "P3")

    p2 = _vector_from_source_node(tree, p2_node) if p2_node else p1.copy()
    p3 = _vector_from_source_node(tree, p3_node) if p3_node else p2.copy()

    use_p2 = bool(bezier_node.inputs["Use P2 (Quadratic)"].default_value) if "Use P2 (Quadratic)" in bezier_node.inputs else False
    use_p3 = bool(bezier_node.inputs["Use P3 (Cubic)"].default_value) if "Use P3 (Cubic)" in bezier_node.inputs else False

    if use_p3:
        end_pt = p3
        h1 = p1
        h2 = p2
    elif use_p2:
        end_pt = p2
        h1 = p0 + (2.0 / 3.0) * (p1 - p0)
        h2 = end_pt + (2.0 / 3.0) * (p1 - end_pt)
    else:
        end_pt = p1
        d = end_pt - p0
        h1 = p0 + (1.0 / 3.0) * d
        h2 = p0 + (2.0 / 3.0) * d

    return p0, h1, h2, end_pt


def _eval_cubic_bezier(p0, h1, h2, p3, u):
    omt = 1.0 - u
    return (
        (omt ** 3) * p0
        + 3.0 * (omt ** 2) * u * h1
        + 3.0 * omt * (u ** 2) * h2
        + (u ** 3) * p3
    )


def build_group():
    if GROUP_NAME in bpy.data.node_groups:
        ng = bpy.data.node_groups[GROUP_NAME]
        ng.nodes.clear()
    else:
        ng = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")

    # --- Hidden random stored on node group (ADDED) ---
    # Stored as string to avoid precision/size issues for huge integers.
    digits = DEFAULT_DIGITS
    n = rand_with_digits(digits)
    ng[HIDDEN_PROP] = str(n)
    ng[DIGITS_PROP] = int(digits)
    ng[MIN_PROP] = str(10 ** (digits - 1))
    ng[MAX_PROP] = str(10 ** digits - 1)

    iface = ng.interface
    ensure_socket(iface, "Geometry", "OUTPUT", "NodeSocketGeometry")
    ensure_socket(iface, "Curve Geometry", "INPUT", "NodeSocketGeometry")
    ensure_socket(iface, "Matrix", "INPUT", "NodeSocketMatrix")
    ensure_socket(iface, "s Min", "INPUT", "NodeSocketFloat", 0.0)
    ensure_socket(iface, "s Max", "INPUT", "NodeSocketFloat", 1.0)
    ensure_socket(iface, "Resolution", "INPUT", "NodeSocketInt", 32)

    nodes = ng.nodes
    links = ng.links

    n_in = nodes.new("NodeGroupInput")
    n_in.location = (0, 0)
    n_out = nodes.new("NodeGroupOutput")
    n_out.location = (500, 0)

    # Pass through curve geometry
    links.new(n_in.outputs["Curve Geometry"], n_out.inputs["Geometry"])

    return ng


class NODE_OT_add_calculate_surface_gn(bpy.types.Operator):
    bl_idname = "node.add_calculate_surface_gn"
    bl_label = "Calculate Surface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ng = build_group()
        tree = context.space_data.edit_tree
        node = tree.nodes.new("GeometryNodeGroup")
        node.node_tree = ng
        node.label = GROUP_NAME
        node.location = getattr(context.space_data, "cursor_location", (0, 0))

        # Auto-wire to root Group Output
        out = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
        if out and "Geometry" in out.inputs and "Geometry" in node.outputs:
            tree.links.new(node.outputs["Geometry"], out.inputs["Geometry"])

        return {"FINISHED"}


class GEOMETRY_OT_calculate_surface(bpy.types.Operator):
    bl_idname = "geometry.build_calculate_surface"
    bl_label = "Build Surface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.active

        if (
            not node
            or node.bl_idname != "GeometryNodeGroup"
            or not node.node_tree
            or node.node_tree.name != GROUP_NAME
        ):
            self.report({"ERROR"}, "Select the 'Calculate Surface' node")
            return {"CANCELLED"}

        curve_node = _find_curve_source_node(tree, node)
        if not curve_node:
            self.report({"ERROR"}, "Connect a Parametric Curve or Bezier Curve to Curve Geometry input")
            return {"CANCELLED"}

        curve_mode = None
        if _is_parametric_curve_node(curve_node):
            curve_mode = "parametric"
        elif _is_bezier_curve_node(curve_node):
            curve_mode = "bezier"
        else:
            self.report({"ERROR"}, "Connect a Parametric Curve or Bezier Curve to Curve Geometry input")
            return {"CANCELLED"}

        matrix_node = _find_matrix_node(tree, node)
        if not matrix_node:
            self.report(
                {"ERROR"}, "Connect a Parametric Transformation Matrix to Matrix input"
            )
            return {"CANCELLED"}

        if curve_mode == "parametric":
            x_expr = curve_node.get("x_expr", "cos(t)")
            y_expr = curve_node.get("y_expr", "sin(t)")
            z_expr = curve_node.get("z_expr", "t/(2*pi)")
            t_min = (
                curve_node.inputs["t Min"].default_value
                if "t Min" in curve_node.inputs
                else 0.0
            )
            t_max = (
                curve_node.inputs["t Max"].default_value
                if "t Max" in curve_node.inputs
                else 1.0
            )
        else:
            x_expr = y_expr = z_expr = None
            t_min = 0.0
            t_max = 1.0

        m_exprs = {}
        for i in range(16):
            key = f"m{i // 4}{i % 4}"
            m_exprs[key] = matrix_node.get(key, "1" if i % 5 == 0 else "0")

        s_min = node.inputs["s Min"].default_value if "s Min" in node.inputs else 0.0
        s_max = node.inputs["s Max"].default_value if "s Max" in node.inputs else 1.0
        resolution = (
            int(node.inputs["Resolution"].default_value)
            if "Resolution" in node.inputs
            else 32
        )
        resolution = max(2, resolution)
        sweep_count = 50

        try:
            if curve_mode == "parametric":
                x_rpn = to_rpn(x_expr)
                y_rpn = to_rpn(y_expr)
                z_rpn = to_rpn(z_expr)
                sampled_curve_points = None
            else:
                bez_p0, bez_h1, bez_h2, bez_p3 = _extract_bezier_controls(tree, curve_node)

            m_rpn = {}
            for key, expr in m_exprs.items():
                m_rpn[key] = to_rpn(expr)

            obj_name = f"Surface_{node.name}"
            if obj_name in bpy.data.objects:
                old_obj = bpy.data.objects[obj_name]
                bpy.data.objects.remove(old_obj, do_unlink=True)

            mesh_data = bpy.data.meshes.new(obj_name + "_mesh")
            mesh_obj = bpy.data.objects.new(obj_name, mesh_data)
            (getattr(context, 'collection', None) or context.scene.collection).objects.link(mesh_obj)

            bm = bmesh.new()
            vertices = []

            for s_idx in range(sweep_count):
                s = s_min + (s_idx / max(1, sweep_count - 1)) * (s_max - s_min)
                row_verts = []

                for t_idx in range(resolution):
                    t = (
                        t_min + (t_idx / max(1, resolution - 1)) * (t_max - t_min)
                        if resolution > 1
                        else t_min
                    )

                    if curve_mode == "parametric":
                        x = eval_rpn(x_rpn, {"t": t})
                        y = eval_rpn(y_rpn, {"t": t})
                        z = eval_rpn(z_rpn, {"t": t})
                    else:
                        u = t_idx / max(1, resolution - 1)
                        pt = _eval_cubic_bezier(bez_p0, bez_h1, bez_h2, bez_p3, u)
                        x, y, z = pt.x, pt.y, pt.z

                    matrix_vals = {}
                    for key, rpn in m_rpn.items():
                        matrix_vals[key] = eval_rpn(rpn, {"s": s})

                    m = Matrix(
                        [
                            [matrix_vals.get("m00", 0), matrix_vals.get("m01", 0), matrix_vals.get("m02", 0), matrix_vals.get("m03", 0)],
                            [matrix_vals.get("m10", 0), matrix_vals.get("m11", 0), matrix_vals.get("m12", 0), matrix_vals.get("m13", 0)],
                            [matrix_vals.get("m20", 0), matrix_vals.get("m21", 0), matrix_vals.get("m22", 0), matrix_vals.get("m23", 0)],
                            [matrix_vals.get("m30", 0), matrix_vals.get("m31", 0), matrix_vals.get("m32", 0), matrix_vals.get("m33", 1)],
                        ]
                    )

                    p = Vector([x, y, z, 1])
                    p_transformed = m @ p

                    if p_transformed.w != 0:
                        v = bm.verts.new((p_transformed.x / p_transformed.w, p_transformed.y / p_transformed.w, p_transformed.z / p_transformed.w))
                    else:
                        v = bm.verts.new((p_transformed.x, p_transformed.y, p_transformed.z))

                    row_verts.append(v)

                vertices.append(row_verts)

            for s_idx in range(sweep_count - 1):
                for t_idx in range(resolution - 1):
                    v1 = vertices[s_idx][t_idx]
                    v2 = vertices[s_idx][t_idx + 1]
                    v3 = vertices[s_idx + 1][t_idx + 1]
                    v4 = vertices[s_idx + 1][t_idx]
                    try:
                        bm.faces.new([v1, v2, v3, v4])
                    except Exception:
                        pass

            bm.to_mesh(mesh_data)
            bm.free()
            mesh_data.update()

            context.view_layer.objects.active = mesh_obj
            mesh_obj.select_set(True)

            self.report({"INFO"}, f"Surface created: {sweep_count}x{resolution} grid")
            return {"FINISHED"}

        except Exception as ex:
            self.report({"ERROR"}, f"Error: {str(ex)}")
            import traceback
            traceback.print_exc()
            return {"CANCELLED"}


class NODE_PT_calculate_surface(bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Calculate Surface"
    bl_label = "Calculate Surface"

    @classmethod
    def poll(cls, context):
        tree = getattr(context.space_data, "edit_tree", None)
        node = getattr(tree, "nodes", None)
        node = tree.nodes.active if tree else None
        return bool(
            node
            and node.bl_idname == "GeometryNodeGroup"
            and node.node_tree
            and node.node_tree.name == GROUP_NAME
        )

    def draw(self, context):
        layout = self.layout
        layout.operator("geometry.build_calculate_surface", icon="MESH_DATA")


def menu_func(self, context):
    if context.space_data and context.space_data.tree_type == "GeometryNodeTree":
        self.layout.operator(
            NODE_OT_add_calculate_surface_gn.bl_idname, text="Calculate Surface"
        )


classes = (
    NODE_OT_add_calculate_surface_gn,
    GEOMETRY_OT_calculate_surface,
    NODE_PT_calculate_surface,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.NODE_MT_add.append(menu_func)


def unregister():
    bpy.types.NODE_MT_add.remove(menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
