bl_info = {
    "name": "Parametric Surface",
    "author": "Alessio Fumagalli",
    "version": (1, 1, 0),
    "blender": (4, 5, 0),
    "location": "Geometry Nodes > Add",
    "description": "Parametric surface primitive (x(u,v), y(u,v), z(u,v)) built from native Geometry Nodes",
    "category": "Node",
}

import bpy
from bpy.props import StringProperty, CollectionProperty
from math import pi, e, tau
import re
import random  # <<< ADDED (only for hidden random)

GROUP_NAME = "Parametric Surface"

# --- Hidden random (ADDED) ---
DEFAULT_DIGITS = 100
HIDDEN_PROP = "hidden_random"
DIGITS_PROP = "hidden_random_digits"
MIN_PROP = "hidden_random_min"
MAX_PROP = "hidden_random_max"
GROUP_MARKER = "_parametric_surface_group"
SETTINGS_COLLECTION = "parametric_surface_node_settings"

DEFAULT_X_EXPR = "sin(v)*cos(u)"
DEFAULT_Y_EXPR = "sin(v)*sin(u)"
DEFAULT_Z_EXPR = "cos(v)"


def rand_with_digits(digits: int) -> int:
    if not isinstance(digits, int) or digits < 1:
        raise ValueError("digits must be an integer >= 1")
    lo = 10 ** (digits - 1)
    hi = 10 ** digits - 1
    return random.randint(lo, hi)



# -------------------------------------------------------------------
# Compatibility helpers for Blender 4.5 LTS -> Blender 5.x
# -------------------------------------------------------------------
def get_editor_tree(context):
    """Return the node tree currently edited in the Node Editor."""
    space = getattr(context, "space_data", None)
    if not space:
        return None
    return getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)


def is_parametric_surface_node(node):
    """Recognize new nodes and nodes created by the old v1.0.x add-on."""
    if not node or node.bl_idname != "GeometryNodeGroup" or not node.node_tree:
        return False

    ng = node.node_tree
    try:
        if bool(ng.get(GROUP_MARKER, False)):
            return True
    except Exception:
        pass

    # Backward compatibility for old files, where no explicit marker existed.
    return ng.name.startswith(GROUP_NAME)


def get_active_parametric_node(context):
    """Resolve the active node consistently across Blender 4.5 and 5.x."""
    tree = get_editor_tree(context)
    if tree is None:
        return None

    node = getattr(context, "active_node", None)
    if node is None or getattr(node, "id_data", None) != tree:
        node = getattr(tree.nodes, "active", None)

    return node if is_parametric_surface_node(node) else None


class ParametricSurfaceNodeSettings(bpy.types.PropertyGroup):
    """Per Parametric Surface node expressions, stored on the parent NodeTree.

    NodeTree is an ID data-block, so this avoids the pre-5.0 ambiguity of using
    raw custom properties directly on GeometryNodeGroup node instances.
    """

    node_name: StringProperty(name="Node", default="")
    x_expr: StringProperty(name="x(u,v)", default=DEFAULT_X_EXPR)
    y_expr: StringProperty(name="y(u,v)", default=DEFAULT_Y_EXPR)
    z_expr: StringProperty(name="z(u,v)", default=DEFAULT_Z_EXPR)


def get_surface_settings(parent_tree, node, create=True):
    """Get/create the expression record belonging to one node instance."""
    if parent_tree is None or node is None:
        return None

    settings_collection = getattr(parent_tree, SETTINGS_COLLECTION, None)
    if settings_collection is None:
        return None

    # Node names are unique inside one NodeTree, so this is per-instance.
    for item in settings_collection:
        if item.node_name == node.name:
            return item

    if not create:
        return None

    item = settings_collection.add()
    item.node_name = node.name

    # One-time migration from v1.0.x raw node custom properties.
    try:
        item.x_expr = str(node.get("x_expr", DEFAULT_X_EXPR))
        item.y_expr = str(node.get("y_expr", DEFAULT_Y_EXPR))
        item.z_expr = str(node.get("z_expr", DEFAULT_Z_EXPR))
    except Exception:
        item.x_expr = DEFAULT_X_EXPR
        item.y_expr = DEFAULT_Y_EXPR
        item.z_expr = DEFAULT_Z_EXPR

    return item


def ensure_node_is_active(tree, node):
    """Make active-node state explicit; Blender 4.5 can otherwise lag here."""
    if tree is None or node is None:
        return
    for other in tree.nodes:
        other.select = False
    node.select = True
    tree.nodes.active = node


def make_node_tree_single_user(node):
    """Fork a shared group before rebuilding, so duplicated nodes stay independent."""
    if node and node.node_tree and node.node_tree.users > 1:
        node.node_tree = node.node_tree.copy()
        node.node_tree[GROUP_MARKER] = True


# -----------------------------
# Utility: create a Value node
# -----------------------------
def make_value(nodes, val, loc=(0, 0)):
    n = nodes.new("ShaderNodeValue")
    n.outputs[0].default_value = float(val)
    n.location = loc
    return n, n.outputs[0]


# --------------------------------------
# Shunting-yard tokenizer & parser (RPN)
# --------------------------------------
_TOKEN_SPEC = [
    ("NUMBER", r"\d+(\.\d+)?"),  # 12, 12.34
    ("ID", r"[A-Za-z_]\w*"),  # identifiers
    ("OP", r"[\+\-\*/\^\(\),]"),  # operators and delimiters
    ("SKIP", r"[ \t]+"),  # spaces
]
_TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _TOKEN_SPEC))


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


# Operator precedence & associativity
_PRECEDENCE = {
    "^": (4, "right"),
    "*": (3, "left"),
    "/": (3, "left"),
    "+": (2, "left"),
    "-": (2, "left"),
}

# function arity map
_FUNC_1 = {
    "sin", "cos", "tan", "asin", "acos", "atan",
    "sqrt", "abs", "exp", "ln", "floor", "ceil", "frac",
}
_FUNC_2 = {"pow", "log", "min", "max", "mod"}


def to_rpn(expr):
    expr = expr.strip()
    if not expr:
        raise ValueError("Expression is empty")

    out = []
    stack = []
    arg_count = []  # for multi-arg functions

    prev_token = None
    for typ, txt in tokenize(expr):
        low = txt.lower()

        if typ == "NUMBER":
            out.append(("NUMBER", txt))
        elif typ == "ID":
            out.append(("ID", txt)) if prev_token and prev_token[1] == ")" else out.append(("ID", txt))
            out[-1] = ("ID", txt)
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
                if not stack:
                    raise ValueError("Misplaced comma or mismatched parentheses")
                if not arg_count:
                    raise ValueError("Comma outside of function call")
                arg_count[-1] += 1
            elif txt == ")":
                while stack and not (stack[-1][0] == "OP" and stack[-1][1] == "("):
                    out.append(stack.pop())
                if not stack:
                    raise ValueError("Mismatched parentheses")
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
                        if top[0] == "OP" and top[1] in _PRECEDENCE:
                            p1, assoc1 = _PRECEDENCE[txt]
                            p2, _ = _PRECEDENCE[top[1]]
                            if (assoc1 == "left" and p1 <= p2) or (assoc1 == "right" and p1 < p2):
                                out.append(stack.pop())
                            else:
                                break
                        elif top[0] == "UNARY":
                            out.append(stack.pop())
                        else:
                            break
                    stack.append(("OP", txt))
        prev_token = (typ, txt)

    while stack:
        tok = stack.pop()
        if tok[0] == "OP" and tok[1] in ("(", ")"):
            raise ValueError("Mismatched parentheses at end")
        out.append(tok)
    return out


# ---------------------------------------------
# Build Math node graph from RPN into GN editor
# ---------------------------------------------
def make_math(nodes, op, a=None, b=None, loc=(0, 0)):
    n = nodes.new("ShaderNodeMath")
    n.operation = op
    n.location = loc
    if a is not None:
        nodes.id_data.links.new(a, n.inputs[0])
    if b is not None:
        nodes.id_data.links.new(b, n.inputs[1])
    return n, n.outputs[0]


def build_expr(nodes, base_x, base_y, source_socket_u, source_socket_v, expr_string):
    rpn = to_rpn(expr_string)
    stack = []
    x = base_x
    y = base_y
    dx = 220
    dy = -140

    def push_socket(s):
        nonlocal x, y
        stack.append((s, x, y))
        y += dy

    def pop2():
        b = stack.pop()[0]
        a = stack.pop()[0]
        return a, b

    for tok in rpn:
        if tok[0] == "NUMBER":
            node, sock = make_value(nodes, float(tok[1]), loc=(x, y))
            push_socket(sock)
            x += dx
        elif tok[0] == "ID":
            name = tok[1].lower()
            if name == "u":
                push_socket(source_socket_u)
                x += dx
            elif name == "v":
                push_socket(source_socket_v)
                x += dx
            elif name in ("pi", "e", "tau"):
                val = {"pi": pi, "e": e, "tau": tau}[name]
                node, sock = make_value(nodes, val, loc=(x, y))
                push_socket(sock)
                x += dx
            else:
                raise ValueError(f"Unknown identifier '{tok[1]}' (use u, v, pi, e, tau, or functions)")
        elif tok[0] == "OP":
            op = tok[1]
            if op in {"+", "-", "*", "/", "^"}:
                a, b = pop2()
                if op == "+":
                    node, sock = make_math(nodes, "ADD", a, b, loc=(x, y))
                elif op == "-":
                    node, sock = make_math(nodes, "SUBTRACT", a, b, loc=(x, y))
                elif op == "*":
                    node, sock = make_math(nodes, "MULTIPLY", a, b, loc=(x, y))
                elif op == "/":
                    node, sock = make_math(nodes, "DIVIDE", a, b, loc=(x, y))
                elif op == "^":
                    node, sock = make_math(nodes, "POWER", a, b, loc=(x, y))
                push_socket(sock)
                x += dx
            else:
                raise ValueError(f"Unsupported operator '{op}'")
        elif tok[0] == "UNARY":
            op = tok[1]
            if op == "neg":
                a = stack.pop()[0]
                node, sock = make_math(nodes, "MULTIPLY", a, None, loc=(x, y))
                node.inputs[1].default_value = -1.0
                push_socket(sock)
                x += dx
            else:
                raise ValueError(f"Unsupported unary operator '{op}'")
        elif tok[0] == "FUNC":
            fname = tok[1].lower()
            nargs = tok[2]
            if fname in _FUNC_1 and nargs == 1:
                a = stack.pop()[0]
                opmap = {
                    "sin": "SINE", "cos": "COSINE", "tan": "TANGENT",
                    "asin": "ARCSINE", "acos": "ARCCOSINE", "atan": "ARCTANGENT",
                    "sqrt": "SQRT", "abs": "ABSOLUTE", "exp": "EXPONENT",
                    "ln": "LOGARITHM", "floor": "FLOOR", "ceil": "CEIL", "frac": "FRACTION",
                }
                if fname == "ln":
                    node, sock = make_math(nodes, "LOGARITHM", a, None, loc=(x, y))
                    node.inputs[1].default_value = e
                else:
                    node, sock = make_math(nodes, opmap[fname], a, None, loc=(x, y))
                push_socket(sock)
                x += dx
            elif fname in _FUNC_2 and nargs == 2:
                b = stack.pop()[0]
                a = stack.pop()[0]
                if fname == "pow":
                    node, sock = make_math(nodes, "POWER", a, b, loc=(x, y))
                elif fname == "log":
                    node, sock = make_math(nodes, "LOGARITHM", a, b, loc=(x, y))
                elif fname == "min":
                    node, sock = make_math(nodes, "MINIMUM", a, b, loc=(x, y))
                elif fname == "max":
                    node, sock = make_math(nodes, "MAXIMUM", a, b, loc=(x, y))
                elif fname == "mod":
                    node, sock = make_math(nodes, "MODULO", a, b, loc=(x, y))
                else:
                    raise ValueError(f"Unsupported function '{fname}'")
                push_socket(sock)
                x += dx
            else:
                raise ValueError(f"Function '{fname}' expects {1 if fname in _FUNC_1 else 2} argument(s)")
        else:
            raise ValueError(f"Token not handled: {tok}")
    if len(stack) != 1:
        raise ValueError("Malformed expression (stack not singular at end)")
    return stack[0][0], x


# -------------------------------
# Interface socket helper
# -------------------------------
def ensure_socket(iface, name, in_out, socket_type, default=None, min_value=None, max_value=None):
    sock = next((s for s in iface.items_tree if s.name == name and getattr(s, "in_out", None) == in_out), None)
    if not sock:
        sock = iface.new_socket(name, socket_type=socket_type, in_out=in_out)
    if default is not None and hasattr(sock, "default_value"):
        sock.default_value = default
    if min_value is not None and hasattr(sock, "min_value"):
        sock.min_value = min_value
    if max_value is not None and hasattr(sock, "max_value"):
        sock.max_value = max_value
    return sock


# -------------------------------
# Socket lookup helper
# -------------------------------
def first_geo_socket(sockets, preferred_names=()):
    for name in preferred_names:
        if name in sockets:
            return sockets[name]
    for s in sockets:
        if getattr(s, "bl_socket_idname", "").lower().endswith("geometry"):
            return s
    return sockets[0] if sockets else None


# ---------------------------------------
# Build the whole node group from scratch
# ---------------------------------------
def build_group_from_expressions(x_expr, y_expr, z_expr, ng=None):
    if ng is None:
        ng = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    else:
        ng.nodes.clear()

    # --- Hidden random stored on node group (ADDED) ---
    # Stored as string to avoid precision/size issues for huge integers.
    digits = DEFAULT_DIGITS
    n = rand_with_digits(digits)
    ng[HIDDEN_PROP] = str(n)
    ng[DIGITS_PROP] = int(digits)
    ng[MIN_PROP] = str(10 ** (digits - 1))
    ng[MAX_PROP] = str(10 ** digits - 1)
    ng[GROUP_MARKER] = True

    iface = ng.interface
    ensure_socket(iface, "Geometry", "OUTPUT", "NodeSocketGeometry")
    ensure_socket(iface, "u Min", "INPUT", "NodeSocketFloat", default=0.0)
    ensure_socket(iface, "u Max", "INPUT", "NodeSocketFloat", default=2 * pi)
    ensure_socket(iface, "v Min", "INPUT", "NodeSocketFloat", default=0.0)
    ensure_socket(iface, "v Max", "INPUT", "NodeSocketFloat", default=pi)
    ensure_socket(iface, "u Resolution", "INPUT", "NodeSocketInt", default=50, min_value=1)
    ensure_socket(iface, "v Resolution", "INPUT", "NodeSocketInt", default=50, min_value=1)

    nodes = ng.nodes
    links = ng.links
    n_in = nodes.new("NodeGroupInput")
    n_in.location = (-1600, 200)
    n_out = nodes.new("NodeGroupOutput")
    n_out.location = (1000, 200)

    # (1) Create Grid Mesh
    n_grid = nodes.new("GeometryNodeMeshGrid")
    n_grid.location = (-1400, 400)
    links.new(n_in.outputs["u Resolution"], n_grid.inputs["Vertices X"])
    links.new(n_in.outputs["v Resolution"], n_grid.inputs["Vertices Y"])

    # (2) Separate UV Map into u and v components
    n_sep_uv = nodes.new("ShaderNodeSeparateXYZ")
    n_sep_uv.location = (-1200, 600)
    links.new(n_grid.outputs["UV Map"], n_sep_uv.inputs["Vector"])

    # u = uv.x * (u_max - u_min) + u_min
    n_u_span = nodes.new("ShaderNodeMath")
    n_u_span.operation = "SUBTRACT"
    n_u_span.location = (-1000, 800)
    links.new(n_in.outputs["u Max"], n_u_span.inputs[0])
    links.new(n_in.outputs["u Min"], n_u_span.inputs[1])

    n_u_scaled = nodes.new("ShaderNodeMath")
    n_u_scaled.operation = "MULTIPLY"
    n_u_scaled.location = (-800, 700)
    links.new(n_sep_uv.outputs["X"], n_u_scaled.inputs[0])
    links.new(n_u_span.outputs[0], n_u_scaled.inputs[1])

    n_u = nodes.new("ShaderNodeMath")
    n_u.operation = "ADD"
    n_u.location = (-600, 700)
    links.new(n_in.outputs["u Min"], n_u.inputs[0])
    links.new(n_u_scaled.outputs[0], n_u.inputs[1])

    # v = uv.y * (v_max - v_min) + v_min
    n_v_span = nodes.new("ShaderNodeMath")
    n_v_span.operation = "SUBTRACT"
    n_v_span.location = (-1000, 500)
    links.new(n_in.outputs["v Max"], n_v_span.inputs[0])
    links.new(n_in.outputs["v Min"], n_v_span.inputs[1])

    n_v_scaled = nodes.new("ShaderNodeMath")
    n_v_scaled.operation = "MULTIPLY"
    n_v_scaled.location = (-800, 400)
    links.new(n_sep_uv.outputs["Y"], n_v_scaled.inputs[0])
    links.new(n_v_span.outputs[0], n_v_scaled.inputs[1])

    n_v = nodes.new("ShaderNodeMath")
    n_v.operation = "ADD"
    n_v.location = (-600, 400)
    links.new(n_in.outputs["v Min"], n_v.inputs[0])
    links.new(n_v_scaled.outputs[0], n_v.inputs[1])

    # (3) Build x(u,v), y(u,v), z(u,v) graphs
    try:
        x_sock, xr = build_expr(nodes, base_x=-200, base_y=400, source_socket_u=n_u.outputs[0], source_socket_v=n_v.outputs[0], expr_string=x_expr)
        y_sock, yr = build_expr(nodes, base_x=-200, base_y=100, source_socket_u=n_u.outputs[0], source_socket_v=n_v.outputs[0], expr_string=y_expr)
        z_sock, zr = build_expr(nodes, base_x=-200, base_y=-200, source_socket_u=n_u.outputs[0], source_socket_v=n_v.outputs[0], expr_string=z_expr)
    except Exception as ex:
        raise

    n_combine = nodes.new("ShaderNodeCombineXYZ")
    n_combine.location = (max(xr, yr, zr) + 60, 100)
    links.new(x_sock, n_combine.inputs[0])
    links.new(y_sock, n_combine.inputs[1])
    links.new(z_sock, n_combine.inputs[2])

    # (5) Set position and output
    n_setpos = nodes.new("GeometryNodeSetPosition")
    n_setpos.location = (max(xr, yr, zr) + 280, 100)
    links.new(n_grid.outputs["Mesh"], n_setpos.inputs["Geometry"])
    links.new(n_combine.outputs["Vector"], n_setpos.inputs["Position"])

    links.new(n_setpos.outputs["Geometry"], n_out.inputs["Geometry"])

    return ng


# ---------------------------------------
# Operator: Add the GN group into the tree
# ---------------------------------------
class NODE_OT_add_parametric_surface_gn(bpy.types.Operator):
    bl_idname = "node.add_parametric_surface_gn"
    bl_label = "Parametric Surface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        tree = get_editor_tree(context)
        if tree is None:
            self.report({"ERROR"}, "No editable Geometry Nodes tree found")
            return {"CANCELLED"}

        ng = build_group_from_expressions(DEFAULT_X_EXPR, DEFAULT_Y_EXPR, DEFAULT_Z_EXPR)
        node = tree.nodes.new("GeometryNodeGroup")
        node.node_tree = ng
        node.label = GROUP_NAME
        node.location = getattr(context.space_data, "cursor_location", (0, 0))

        # Create a dedicated formula record for this exact node instance.
        settings = get_surface_settings(tree, node, create=True)
        if settings is not None:
            settings.x_expr = DEFAULT_X_EXPR
            settings.y_expr = DEFAULT_Y_EXPR
            settings.z_expr = DEFAULT_Z_EXPR

        # Important on Blender 4.5: nodes.new() does not always leave the new
        # scripted node as the active node seen by a custom sidebar panel.
        ensure_node_is_active(tree, node)

        out = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
        if out and "Geometry" in out.inputs and "Geometry" in node.outputs:
            tree.links.new(node.outputs["Geometry"], out.inputs["Geometry"])
        return {"FINISHED"}


# ---------------------------------------
# Operator: (Re)build from expressions UI
# ---------------------------------------
class NODE_OT_build_parametric_surface(bpy.types.Operator):
    bl_idname = "node.build_parametric_surface_gn"
    bl_label = "Build Surface"
    bl_options = {"REGISTER", "UNDO"}

    # Passed by the panel so Blender 4.5 cannot accidentally rebuild a stale
    # active node when more than one Parametric Surface exists.
    parent_tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def _resolve_tree_and_node(self, context):
        tree = get_editor_tree(context)

        if self.parent_tree_name:
            candidate = bpy.data.node_groups.get(self.parent_tree_name)
            if candidate is not None:
                tree = candidate

        if tree is not None and self.node_name:
            node = tree.nodes.get(self.node_name)
            if is_parametric_surface_node(node):
                return tree, node

        node = get_active_parametric_node(context)
        return tree, node

    def execute(self, context):
        tree, node = self._resolve_tree_and_node(context)
        if tree is None or not is_parametric_surface_node(node):
            self.report({"ERROR"}, "Select the 'Parametric Surface' node to build")
            return {"CANCELLED"}

        settings = get_surface_settings(tree, node, create=True)
        if settings is None:
            self.report({"ERROR"}, "Could not access Parametric Surface settings")
            return {"CANCELLED"}

        x_expr = settings.x_expr
        y_expr = settings.y_expr
        z_expr = settings.z_expr

        try:
            # If the GeometryNodeGroup was duplicated, make its internal group
            # single-user before rebuilding. Formulas themselves are already
            # per-node because they live on the parent NodeTree record above.
            make_node_tree_single_user(node)
            ng = build_group_from_expressions(x_expr, y_expr, z_expr, ng=node.node_tree)
        except Exception as ex:
            self.report({"ERROR"}, f"Parse/build error: {ex}")
            return {"CANCELLED"}

        node.node_tree = ng
        ensure_node_is_active(tree, node)
        self.report({"INFO"}, "Parametric surface rebuilt")
        return {"FINISHED"}


# ---------------------------------------
# UI Panel in the GN editor N-panel
# ---------------------------------------
class NODE_PT_parametric_surface(bpy.types.Panel):
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Parametric Surface"
    bl_label = "Parametric Surface"

    @classmethod
    def poll(cls, context):
        return get_active_parametric_node(context) is not None

    def draw(self, context):
        layout = self.layout
        tree = get_editor_tree(context)
        node = get_active_parametric_node(context)
        if tree is None or node is None:
            return

        settings = get_surface_settings(tree, node, create=True)
        if settings is None:
            layout.label(text="Settings unavailable", icon="ERROR")
            return

        col = layout.column(align=True)
        col.prop(settings, "x_expr", text="x(u,v)")
        col.prop(settings, "y_expr", text="y(u,v)")
        col.prop(settings, "z_expr", text="z(u,v)")
        layout.separator()

        op = layout.operator("node.build_parametric_surface_gn", icon="FILE_REFRESH")
        op.parent_tree_name = tree.name
        op.node_name = node.name


# ---------------------------------------
# Menu hook + register
# ---------------------------------------
def menu_func(self, context):
    if context.space_data.tree_type == "GeometryNodeTree":
        self.layout.operator(
            NODE_OT_add_parametric_surface_gn.bl_idname,
            text="Parametric Surface",
            icon="SURFACE_NSURFACE",
        )


classes = (
    ParametricSurfaceNodeSettings,
    NODE_OT_add_parametric_surface_gn,
    NODE_OT_build_parametric_surface,
    NODE_PT_parametric_surface,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)

    # NodeTree is an ID data-block and officially supports registered custom
    # properties. Store one settings record per Parametric Surface node.
    setattr(
        bpy.types.NodeTree,
        SETTINGS_COLLECTION,
        CollectionProperty(type=ParametricSurfaceNodeSettings),
    )

    bpy.types.NODE_MT_add.append(menu_func)


def unregister():
    try:
        bpy.types.NODE_MT_add.remove(menu_func)
    except Exception:
        pass

    if hasattr(bpy.types.NodeTree, SETTINGS_COLLECTION):
        delattr(bpy.types.NodeTree, SETTINGS_COLLECTION)

    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
