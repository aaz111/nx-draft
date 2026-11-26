"""制图函数（自动提取）

从 dsh-nx 的 nxworker.py 提取，去掉 dsh 依赖，可直接在 NX 的 batch 会话里跑。

所有函数依赖注入的运行时对象（见 nxdraft.runtime.Runtime），不再依赖闭包变量：
    rt.NX       NXOpen 模块
    rt.s        NXOpen.Session.GetSession()
    rt.state    可变字典，rt.state["part"] 是当前部件
    rt.out(*a)  日志
"""


def fully_loaded(component, budget=None):
    """Return the component's prototype part with its geometry actually in memory.

    A large assembly opens its components minimally loaded: Part.Bodies is empty until
    the prototype is pulled in, which made a 35-component fixture preview as six loose
    screws. Loading is per component and the counter keeps a giant assembly from
    stalling the whole export.
    """
    if budget is None:
        budget = fully_loaded.budget
    prototype = component.Prototype
    if prototype is None or fully_loaded.seen >= budget:
        return prototype
    fully_loaded.seen += 1
    try:
        if not prototype.IsFullyLoaded:
            prototype.LoadFully()
    except Exception:
        pass
    return prototype
fully_loaded.seen = 0
fully_loaded.budget = 40

# Paper size in mm, for laying views out when the wizard refuses (A-series landscape).
SHEET_SIZES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0),
               "A1": (841.0, 594.0), "A0": (1189.0, 841.0)}


def bodies_of(part, names=None):
    bodies = list(part.Bodies)
    if not names:
        return bodies
    wanted = {str(n) for n in names}
    picked = [b for b in bodies if fix_text(getattr(b, "Name", "")) in wanted]
    if not picked:
        raise RuntimeError("no body matches %r" % (names,))
    return picked


def measure(part, names=None):
    bodies = bodies_of(part, names)
    if not bodies:
        names = []
        try:
            root = part.ComponentAssembly.RootComponent
            for child in root.GetChildren():
                names.append(fix_text(getattr(child, "Name", "") or ""))
        except Exception:
            pass
        return {
            "assembly": True,
            "bodies": 0,
            "components": len(names),
            "componentNames": names[:60],
            "note": "assembly has no bodies of its own; measure each component part instead",
        }
    try:
        box = s.Measurement.GetBoundingBoxProperties(bodies, 0, NX.Point3d(0.0, 0.0, 0.0), False)
    except Exception as error:
        raise RuntimeError("bounding box failed: %s" % error)
    result = {
        "bodies": len(bodies),
        "bboxMin": [box[0][0].X, box[0][0].Y, box[0][0].Z],
        "bboxMax": [box[0][6].X, box[0][6].Y, box[0][6].Z],
        "bboxSize": list(box[2]),
        "bboxVolume": float(box[5]),
    }
    props = s.Measurement.GetBodyProperties(bodies, 0.999, False)
    result.update({
        # mass_props[0] is surface area, [1] volume, [2] mass in kg.
        "volume": float(props[0][1]),
        "area": float(props[0][0]),
        "mass": float(props[0][2]),
        "centroid": [props[2].X, props[2].Y, props[2].Z],
        "density": float(props[4]),
    })
    return result


def sheet_info(sheet):
    views = []
    try:
        for v in sheet.GetDraftingViews():
            views.append({
                "name": fix_text(v.Name),
                "kind": type(v).__name__,
                "scale": round(true_scale(v), 4),
                "internalScale": round(float(v.Scale), 4),
            })
    except Exception as e:
        out("sheet views failed:", repr(e))
    scale = (1.0, 1.0)
    try:
        scale = sheet.GetScale()
    except Exception:
        pass
    return {
        "name": fix_text(sheet.Name),
        "width": float(sheet.Length),
        "height": float(sheet.Height),
        "scale": [float(scale[0]), float(scale[1])],
        "projection": str(sheet.ProjectionAngle),
        "views": views,
    }


def current_sheet(part):
    """The part's current drawing sheet, activating a usable one when NX says None.

    `DrawingSheets.CurrentDrawingSheet` returns None for a part whose sheet exists but
    has never been made current in this session -- a part opened straight from disk, for
    instance 阀体1.prt.  Every caller then blew up with "'NoneType' object has no
    attribute 'Name'".  The sheet is still there in the collection, so one is activated
    and returned, which is what NX itself does when the user opens the drawing.

    When a part carries several sheets, one whose views NX refuses to query ("The view is
    not displayed" -- a sheet that was never displayed in this session) is a poor choice:
    nothing can be dimensioned or printed from it.  A sheet with at least one queryable
    view is preferred, falling back to the first sheet.
    """
    try:
        sheet = part.DraftingDrawingSheets.CurrentDrawingSheet
    except Exception:
        sheet = None
    try:
        sheets = list(part.DraftingDrawingSheets)
    except Exception:
        sheets = []

    def usable(candidate):
        """A sheet is usable when at least one of its views can be queried."""
        try:
            for view in candidate.GetDraftingViews():
                try:
                    list(view.AskVisibleObjects())
                    return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    if sheet is not None and (len(sheets) <= 1 or usable(sheet)):
        return sheet
    if not sheets:
        return None
    chosen = sheet if sheet is not None else sheets[0]
    for candidate in sheets:
        if usable(candidate):
            chosen = candidate
            break
    try:
        chosen.ActivateForSketching()
    except Exception:
        try:
            part.DraftingDrawingSheets.SetCurrentSheet(chosen)
        except Exception as error:
            out("current_sheet: could not activate %r: %r"
                % (getattr(chosen, "Name", "?"), error))
    try:
        return part.DraftingDrawingSheets.CurrentDrawingSheet or chosen
    except Exception:
        return chosen


def sheet_list(part):
    sheets = []
    try:
        current = current_sheet(part)
    except Exception:
        current = None
    for sh in list(part.DraftingDrawingSheets):
        info = sheet_info(sh)
        info["current"] = current is not None and sh.Tag == current.Tag
        sheets.append(info)
    return sheets


def views_on_sheet(part, sheet):
    """Visible draft views of a sheet, falling back to the part's own collection.

    A sheet the builder has just created does not always list its views yet, and the layout
    then had nothing to place while the sheet did carry a full set of views.
    """
    def visible(views):
        return [v for v in views if (view_summary(v)["visible"] or 0) > 0]

    try:
        found = visible(sheet.GetDraftingViews())
        if found:
            return found
    except Exception:
        pass
    try:
        return visible(part.DraftingViews)
    except Exception:
        return []

# UF_MODL_ask_face_data face types that mean "there is a hole or a pocket in here".
INTERNAL_FACE_TYPES = (16, 17)   # cylindrical, conical


def has_internal_features(part):
    """True when the solid carries a real hole or bore, not just an outside cylinder.

    SolidFaceType 2 is "bounded cylinder", which a shaft's own outer wall is as well --
    keying the section view off that made every turned part grow a section it did not
    need.  Face.GetHoleData() is what separates the two: a bored wall answers with a
    (ResizeHoleData, True) tuple, a shaft's outer wall answers None.
    """
    try:
        bodies = list(part.Bodies)
    except Exception:
        return False
    for body in bodies:
        try:
            faces = list(body.GetFaces())
        except Exception:
            continue
        for face in faces:
            try:
                kind = face.SolidFaceType
                try:
                    kind = kind.value
                except Exception:
                    pass
                if int(kind) not in (2, 3):
                    continue
            except Exception:
                continue
            try:
                if face.GetHoleData() is not None:
                    return True
            except Exception:
                # Unknown rather than "no": err towards showing the inside.
                return True
    return False


def bores_visible_in_model_views(part, view_names):
    """True when every bore would read as a circle in one of these modelling views.

    The sheet-based test cannot answer this before the views exist: with no view on the
    sheet it returns False, which is how FRONT came to be dropped for a section that was
    then refused -- a turned bushing ended up with ONE view and no way to dimension its
    length.  This variant asks the question about the views that are about to be created,
    so the decision to drop the elevation and the decision to cut it open agree.
    """
    holes = hole_axes(part)
    if not holes:
        return True
    if len({diameter for diameter, _ in holes}) > 2:
        return False                      # a stepped cavity: only a cut shows it
    try:
        model_views = {fix_text(mv.Name).upper(): mv for mv in list(part.ModelingViews)}
    except Exception:
        return False
    for name in view_names:
        mv = model_views.get(str(name).upper())
        if mv is None:
            continue
        try:
            sight = dominant_axis(view_normal(mv.Matrix))
        except Exception:
            continue
        if sight is None:
            continue
        for diameter, axis in holes:
            if dominant_axis(axis) == sight:
                return True
    return False


def bores_already_visible(part, sheet):
    """True when every bore on the part shows as a circle in a view already on the sheet.

    This is what decides whether a section earns its place.  A plate with two through
    holes already shows both as circles in Top, so a section through it only repeats the
    outline.  A part with a real CAVITY is different: the bore is a stepped, internal
    shape that a circle in plan does not describe, and only a cut reveals it -- so the
    test asks for a plain through bore, not merely for a circle somewhere.
    """
    holes = hole_axes(part)
    if not holes:
        return True                       # nothing internal to hide
    # A deep bore with more than one diameter is a cavity, not a drilled hole: the plan
    # view shows concentric circles but says nothing about the depths or the shoulders.
    if len({diameter for diameter, _ in holes}) > 2:
        return False
    try:
        views = [v for v in views_on_sheet(part, sheet)]
    except Exception:
        return False
    for view in views:
        try:
            m = view.Matrix
        except Exception:
            continue
        sight = dominant_axis((float(m.Zx), float(m.Zy), float(m.Zz)))
        if sight is None:
            continue
        for diameter, axis in holes:
            if dominant_axis(axis) == sight:
                return True               # at least one bore reads as a circle here
    return False


def ensure_section_view(part, sheet, replace=True):
    """Section through the middle of the part, when a view cannot show the inside.

    A section is not a size carrier -- it repeats what the outline already says -- so it
    only earns its place when a bore is invisible in every view on the sheet.  On a plate
    whose holes are already drawn as circles in Top, a section adds nothing and is skipped.

    The parent is whichever view the section can be cut from: FRONT when it exists,
    otherwise TOP.  A cavity part is drawn without a FRONT at all (the section replaces
    it), so the cut has to hang off the plan instead.
    """
    if not has_internal_features(part):
        return {"created": False, "reason": "no internal feature (no bore)"}
    try:
        covered = bores_already_visible(part, sheet)
    except Exception:
        covered = False
    if covered:
        return {"created": False,
                "reason": "every bore is already shown as a circle in an existing view"}
    # Pick a parent that is actually on the sheet.
    parent = None
    for candidate in ("FRONT", "TOP", "RIGHT"):
        for view in views_on_sheet(part, sheet):
            if fix_text(view.Name).split("@")[0].upper() == candidate:
                parent = candidate
                break
        if parent:
            break
    if parent is None:
        return {"created": False, "reason": "no orthographic view to cut from"}
    try:
        box = measure(part)
        lo = box.get("bboxMin") or [0.0, 0.0, 0.0]
        hi = box.get("bboxMax") or [0.0, 0.0, 0.0]
    except Exception as error:
        return {"created": False, "reason": "no bounding box: %s" % error}

    # ---- WHERE to cut.
    #
    # The cut used to run through the middle of the bounding box along the longer
    # horizontal axis, which is a guess about the part's shape rather than a reading of it.
    # On the pump body it cut at the midpoint of Z -- which is exactly the plane where the
    # Ø20 side bore meets the Ø68 cavity, so the section showed those two and MISSED the
    # Ø38 cavity and the M16 thread above it: features the drawing then had no way to
    # dimension.  A section earns its place by exposing internal features, so the plane is
    # chosen to pass through as many of them as possible: every candidate position (each
    # distinct feature coordinate, plus the midpoint as a fallback) is scored by how many
    # bore centres and how many internal faces it would cut through.
    cut_choice = section_cut_plane(part, lo, hi)
    if cut_choice is None:
        return {"created": False, "reason": "no plane could be found to cut along"}
    cut_info = cut_choice.get("info") or {}
    try:
        if cut_choice["kind"] == "vertical":
            # A vertical cut has to go through UF_DRAW_create_simple_sxview: the
            # SectionViewBuilder refuses it with "Invalid hinge line".
            result = cmd_section_vertical({"parent": parent,
                                           "cutX": cut_choice["cutX"]})
            cut_info["direction"] = cut_choice["kind"]
            return {"created": True, "parent": parent,
                    "view": result.get("view", {}).get("name"),
                    "plane": cut_info}
        if cut_choice["kind"] == "vertical-y":
            # A cut at constant model Y is the same operation as one at constant model X;
            # cmd_section_vertical places the cut point along the parent view's Y instead
            # of its X.  Falling back to a horizontal cut here would silently undo the
            # whole point of choosing the direction (the pump body needs the vertical cut
            # to expose its M16 thread and Ø20 side bore).
            result = cmd_section_vertical({"parent": parent,
                                           "cutY": cut_choice["cutY"],
                                           "axis": "y"})
            cut_info["direction"] = cut_choice["kind"]
            return {"created": True, "parent": parent,
                    "view": result.get("view", {}).get("name"),
                    "plane": cut_info}
        else:
            x0, y0, x1, y1, zc = (cut_choice["x0"], cut_choice["y0"],
                                  cut_choice["x1"], cut_choice["y1"], cut_choice["z"])
        result = cmd_section_view({"parent": parent,
                                   "start": [x0, y0, zc],
                                   "end": [x1, y1, zc]})
    except Exception as error:
        out("auto section failed:", repr(error))
        return {"created": False, "reason": str(error)[:200]}
    return {"created": True, "parent": parent,
            "view": result.get("view", {}).get("name"),
            "cut": [[round(x0, 2), round(y0, 2), round(zc, 2)],
                    [round(x1, 2), round(y1, 2), round(zc, 2)]],
            "plane": cut_info}


def section_cut_plane(part, lo, hi):
    """Pick the cut plane that exposes the most internal features.

    Returns a dict describing the chosen cut, or None.

    A section is only worth drawing for what it REVEALS, so the plane is chosen from where
    features actually sit rather than from the middle of the bounding box.  The direction
    of the cut matters more than its position, and it is decided from the bores themselves:

      * a bore whose axis is VERTICAL (Z) is invisible in a horizontal cut -- the plane
        slices it into a plain circle and says nothing about its depth.  It is exposed by a
        VERTICAL cut standing the plane up through the part, which is what turns the pump
        body's M16 thread and Ø20 side bore into readable depths;
      * a bore whose axis is horizontal is the other way round.

    So the cut runs along whichever direction crosses the most bores' axes.  The position
    along that direction is then scored by how many bore centres it passes through, with
    the midpoint kept as a candidate so a featureless part still gets a sensible cut.
    """
    lo_x, lo_y, lo_z = float(lo[0]), float(lo[1]), float(lo[2])
    hi_x, hi_y, hi_z = float(hi[0]), float(hi[1]), float(hi[2])

    # ---- every bore as (centre, axis, diameter)
    bores = []
    try:
        for body in list(part.Bodies):
            for edge in list(body.GetEdges()):
                if edge_class(edge) != 2:
                    continue
                try:
                    vertices = list(edge.GetVertices())
                    if len(vertices) != 2:
                        continue
                    a = vertex_point(vertices[0])
                    b = vertex_point(vertices[1])
                    if (abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6
                            or abs(a[2] - b[2]) > 1e-6):
                        continue
                    arc = drf().UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
                except Exception:
                    continue
                centre = (float(arc.ArcCenter[0]), float(arc.ArcCenter[1]),
                          float(arc.ArcCenter[2]))
                diameter = round(float(arc.Radius) * 2.0, 2)
                bores.append((centre, arc_axis(part, arc, edge), diameter))
    except Exception as error:
        out("section plane: bore scan failed:", repr(error))

    # ---- which direction to cut along.
    #
    # A cut OPENS a bore only when the bore's axis LIES IN the cut plane, i.e. when the
    # axis is perpendicular to the plane's normal.  A bore parallel to the normal is the
    # one thing the cut cannot show -- it comes out as a plain circle.
    #
    # This test used to be the wrong way round ("crossed" meant the axis was NOT parallel
    # to the normal), which selected a vertical-Y cut for the hand crank whose five bores
    # all run along Y: the plane was parallel to every bore, the scan then reported
    # "cuts 0 bore(s)", and because FRONT had already been dropped for the section the
    # part ended up with a single narrow view and no way to dimension its length.
    def opened_by(axis_vector, cut_normal):
        try:
            return abs(sum(float(axis_vector[i]) * cut_normal[i] for i in range(3))) < 0.5
        except Exception:
            return True

    direction_scores = []
    for name, normal in (("vertical-x", (1.0, 0.0, 0.0)),
                         ("vertical-y", (0.0, 1.0, 0.0)),
                         ("horizontal", (0.0, 0.0, 1.0))):
        score = sum(1 for _centre, axis, _d in bores if opened_by(axis, normal))
        direction_scores.append((score, name, normal))
    direction_scores.sort(key=lambda item: -item[0])
    direction_score, direction, normal = direction_scores[0]
    out("section plane: %d bore(s), best direction %s opens %d"
        % (len(bores), direction, direction_score))
    # A cut that opens nothing is not worth the elevation it costs: say so and let the
    # caller keep its views.
    if direction_score <= 0 and bores:
        out("section plane: no direction opens any bore, no section")
        return None

    # ---- position along the cut.
    #
    # vertical-x: the plane sits at a model X and runs in Y; vertical-y: at a model Y and
    # runs in X; horizontal: at a model Z and runs in X.
    if direction == "vertical-x":
        span_lo, span_hi = lo_x, hi_x
        coord_index = 0
    elif direction == "vertical-y":
        span_lo, span_hi = lo_y, hi_y
        coord_index = 1
    else:
        span_lo, span_hi = lo_z, hi_z
        coord_index = 2
    mid = (span_lo + span_hi) / 2.0

    candidates = set([round(mid, 3)])
    for centre, _axis, _diameter in bores:
        coord = centre[coord_index]
        if span_lo - 1e-6 <= coord <= span_hi + 1e-6:
            candidates.add(round(coord, 3))

    best = None
    for coord in sorted(candidates):
        score = 0
        hit = []
        for centre, _axis, diameter in bores:
            # Only a bore whose axis lies in the cut plane is opened by it; a bore running
            # along the cut's own direction stays a hole seen end-on.
            if abs(centre[coord_index] - coord) <= 1e-6:
                score += 1
                hit.append(diameter)
        if (abs(coord - span_lo) < 1e-6) or (abs(coord - span_hi) < 1e-6):
            score -= 100
        rank = (score, -abs(coord - mid))
        if best is None or rank > best[0]:
            best = (rank, coord, score, hit)
    if best is None:
        return None
    _, coord, score, hit = best

    info = {"direction": direction,
            "at": round(coord, 2),
            "features": score,
            "diameters": sorted(set(hit))}
    out("section plane: %s at %g cuts %d bore(s) %s"
        % (direction, coord, score, info["diameters"]))

    if direction == "vertical-x":
        # A vertical cut: the SectionViewBuilder path refuses these ("Invalid hinge line"),
        # so the caller has to go through UF_DRAW_create_simple_sxview -- see
        # cmd_section_vertical, which takes the model X directly.
        return {"kind": "vertical", "axis": "x", "cutX": coord, "info": info}
    if direction == "vertical-y":
        return {"kind": "vertical-y", "axis": "y", "cutY": coord, "info": info}
    zc = coord
    x0, x1 = lo_x - 5.0, hi_x + 5.0
    y0 = y1 = mid
    return {"kind": "horizontal", "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "z": zc, "info": info}


def view_normal(m):
    """The viewing direction of a Matrix3x3, as a unit model vector."""
    return (float(m.Zx), float(m.Zy), float(m.Zz))


def view_geometry(part, modeling_view):
    """(has_straight_edge, axis_span) for a candidate modelling view.

    The check that stops the box arithmetic from over-promising: a cylinder's side view
    mathematically shows two sides, but both outlines are silhouettes carrying no edges,
    so counting it as a size carrier would leave a view nothing can be dimensioned in.
    Two rim circles whose centres project apart do count -- the gap between them is the
    turned length -- and `axis_span` says which model axis that gap runs along (0/1/2),
    or None when there is no such pair.

    That axis is re-derived from the view directions, not assumed: a turned part lying
    on its side has its length along X, not Z.
    """
    try:
        ux, uy = matrix_axes(modeling_view.Matrix)
    except Exception:
        return True, None
    straight = False
    centres = set()
    for body in list(part.Bodies):
        try:
            edges = list(body.GetEdges())
        except Exception:
            continue
        for edge in edges:
            if edge_class(edge) != 2:
                try:
                    vertices = list(edge.GetVertices())
                    if len(vertices) != 2:
                        continue
                    a = vertex_point(vertices[0])
                    b = vertex_point(vertices[1])
                except Exception:
                    continue
                du = sum((b[i] - a[i]) * ux[i] for i in range(3))
                dv = sum((b[i] - a[i]) * uy[i] for i in range(3))
                if du * du + dv * dv > 1e-12:
                    straight = True
                continue
            try:
                arc = drf().UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
            except Exception:
                continue
            centre = arc.ArcCenter
            centres.add((round(sum(centre[i] * ux[i] for i in range(3)), 3),
                         round(sum(centre[i] * uy[i] for i in range(3)), 3)))
    span_axis = None
    if len(centres) >= 2:
        points = sorted(centres)
        du = max(p[0] for p in points) - min(p[0] for p in points)
        dv = max(p[1] for p in points) - min(p[1] for p in points)
        direction = ux if du >= dv else uy
        span_axis = dominant_axis(direction)
    return straight, span_axis


def minimal_views(part):
    """The fewest orthographic views that still let every size be dimensioned.

    The question is not "how many standard views does NX make", it is "what does each
    view actually let me put a number on".  Two different kinds of requirement:

      * a linear size along model axis i  -- visible in any view that does NOT look down
        axis i (looking along it collapses that side to nothing);
      * a diameter -- a circle only reads as a circle in a view that looks down its own
        axis, so a turned part needs the axial view for its OD and a side view for its
        length.

    Views are added greedily, always the one that satisfies the most still-missing
    requirements, and the search stops as soon as nothing new is on offer.  On a
    120x80x20 plate with two holes that gives Top + Front and drops Right; on a turned
    part it gives the axial + side pair and drops everything else.
    """
    try:
        bodies = list(part.Bodies)
    except Exception:
        bodies = []
    if not bodies:
        return None                       # an assembly: leave the caller's choice alone
    try:
        box = measure(part)
        lo = [float(v) for v in (box.get("bboxMin") or [0.0, 0.0, 0.0])]
        hi = [float(v) for v in (box.get("bboxMax") or [0.0, 0.0, 0.0])]
    except Exception:
        return None
    # ---- requirements: ("len", axis) for each real extent, ("dia", d) for each circle
    try:
        circles = circle_features(part)
    except Exception:
        circles = []
    diameters = {diameter for diameter, _ in circles}
    wanted = set()
    for axis in range(3):
        extent = round(hi[axis] - lo[axis], 3)
        if extent <= 0.0:
            continue
        # A turned part's transverse extent *is* its diameter: dimensioning both would
        # put the same number on the drawing twice, and GB forbids that.  Only the
        # extents a circle does not already cover are required.
        if any(abs(extent - diameter) <= 0.01 for diameter in diameters):
            # But a plain plate whose width happens to match a hole is not a turned part:
            # only skip the extent when the part really is round about that axis.
            if not turned_about(part, axis):
                wanted.add(("len", axis))
            continue
        wanted.add(("len", axis))
    for diameter, axis_vector in circles:
        wanted.add(("dia", diameter))
    if not wanted:
        return None
    try:
        model_views = {fix_text(mv.Name).upper(): mv for mv in list(part.ModelingViews)}
    except Exception:
        return None
    candidates = []
    for name in ("TOP", "FRONT", "RIGHT", "LEFT", "BACK", "BOTTOM"):
        mv = model_views.get(name)
        if mv is None:
            continue
        try:
            sight = dominant_axis(view_normal(mv.Matrix))
        except Exception:
            continue
        if sight is None:
            continue
        satisfied = set()
        for kind, key in wanted:
            if kind == "len":
                if key != sight:          # the side along the line of sight collapses
                    satisfied.add((kind, key))
            else:
                for diameter, axis_vector in circles:
                    if diameter != key:
                        continue
                    if dominant_axis(axis_vector) == sight:
                        satisfied.add((kind, key))
        # A silhouette-only view looks like it carries two extents, but the outline is
        # not made of edges the dimension tool can snap to -- a cylinder's side view is
        # exactly that.  Such a view keeps only the turned length: the gap between two
        # rim circles, which runs along `span_axis`.
        try:
            has_straight, span_axis = view_geometry(part, mv)
        except Exception:
            has_straight, span_axis = True, None
        if not has_straight:
            satisfied = {item for item in satisfied if item[0] != "len"}
            if span_axis is not None:
                satisfied.add(("len", span_axis))
        candidates.append((name, satisfied))
    if not candidates:
        return None
    # ---- greedy cover
    chosen = []
    covered = set()
    for _ in range(len(candidates)):
        best, best_gain, best_set = None, 0, set()
        for name, satisfied in candidates:
            if name in chosen:
                continue
            gain = len(satisfied - covered)
            if gain > best_gain:
                best, best_gain, best_set = name, gain, satisfied
        if best is None or best_gain <= 0:
            break
        chosen.append(best)
        covered |= best_set
    if not chosen:
        return None
    # ---- at least TWO views, always.
    #
    # The greedy cover stops as soon as every size has somewhere to be measured, which on a
    # turned part means a single axial view: its circles carry all the diameters and the
    # gap between two rims carries the length.  That is arithmetically true and useless in
    # practice -- the pump folder's 偏心轴, 手摇曲柄 and 柱塞2 each came out as one narrow
    # view with five Ø readings piled on the centre.  A drawing needs a second view to read
    # the part's shape, and it is where the Ø callouts can escape to.  So one more view is
    # added, the one that contributes the most still-uncovered directions.
    if len(chosen) < 2:
        best = None
        for name, satisfied in candidates:
            if name in chosen:
                continue
            gain = len(satisfied - covered)
            if best is None or gain > best[0]:
                best = (gain, name)
        if best is not None:
            chosen.append(best[1])
        else:
            # Every candidate is already chosen (a part with only one modelling view):
            # fall back to whichever view exists, so the caller still gets two views when
            # the model offers them.
            for name, _satisfied in candidates:
                if name not in chosen:
                    chosen.append(name)
                    break
    # A drawing sheet reads plan first, then the elevation beside it.
    order = {"TOP": 0, "FRONT": 1, "RIGHT": 2, "LEFT": 3, "BACK": 4, "BOTTOM": 5}
    return sorted(chosen, key=lambda name: order.get(name, 9))


def turned_about(part, axis):
    """True when the part is a solid of revolution around `axis`.

    Judged from the faces, not the bounding box: a turned part's outer wall is one
    cylindrical face whose axis is `axis`, and it covers the whole extent in the two
    transverse directions.  A plate with a hole through it has flat faces there.
    """
    try:
        bodies = list(part.Bodies)
    except Exception:
        return False
    for body in bodies:
        for face in list(body.GetFaces()):
            try:
                if int(str(face.SolidFaceType)) not in (2, 3):
                    continue
                arc_axis_found = face_axis(part, face)
            except Exception:
                continue
            if arc_axis_found is not None and dominant_axis(arc_axis_found) == axis:
                # The outer wall of a cylinder runs along the axis; if its radius matches
                # the two transverse extents the part is round there.
                return True
    return False


def face_axis(part, face):
    """The axis direction of a cylindrical face, taken from one of its circular edges."""
    try:
        for edge in list(face.GetEdges()):
            if edge_class(edge) != 2:
                continue
            arc = drf().UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
            return arc_axis(part, arc, edge)
    except Exception:
        pass
    return None


def circle_features(part):
    """(diameter, axis) for every full circle on the part -- bores and outer walls alike.

    The outer wall of a turned part counts: its diameter needs a number just as a bore's
    does, and only the axial view can carry it.
    """
    rows = []
    seen = set()
    for body in list(part.Bodies):
        try:
            edges = list(body.GetEdges())
        except Exception:
            continue
        for edge in edges:
            if edge_class(edge) != 2:
                continue
            try:
                vertices = list(edge.GetVertices())
                if len(vertices) != 2:
                    continue
                a = vertex_point(vertices[0])
                b = vertex_point(vertices[1])
                if (abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6
                        or abs(a[2] - b[2]) > 1e-6):
                    continue              # a partial arc is not a diameter
            except Exception:
                continue
            try:
                arc = drf().UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
            except Exception:
                continue
            diameter = round(float(arc.Radius) * 2.0, 3)
            if diameter in seen:
                continue
            seen.add(diameter)
            rows.append((diameter, arc_axis(part, arc, edge)))
    return rows


def dominant_axis(vector):
    """0/1/2 for the axis a unit vector runs along, or None for an oblique direction."""
    best, best_value = None, 0.0
    for index in range(3):
        value = abs(float(vector[index]))
        if value > best_value:
            best, best_value = index, value
    return best if best_value > 0.999 else None


def hole_axes(part):
    """(diameter, axis) per distinct bore -- outer cylinder walls are not holes.

    A shaft's own outer wall is a bounded cylinder too, so SolidFaceType alone would
    invent a "hole" the diameter of the shaft.  Face.GetHoleData() is the discriminator:
    a bored wall answers with a (ResizeHoleData, True) tuple, the outer wall with None.
    """
    rows = []
    seen = set()
    for body in list(part.Bodies):
        try:
            faces = list(body.GetFaces())
        except Exception:
            continue
        boring = set()
        for face in faces:
            try:
                kind = int(str(face.SolidFaceType))
            except Exception:
                continue
            if kind not in (2, 3):
                continue
            try:
                if face.GetHoleData() is not None:
                    boring.add(face.Tag)
            except Exception:
                continue
        if not boring:
            continue
        try:
            edges = list(body.GetEdges())
        except Exception:
            continue
        for edge in edges:
            if edge_class(edge) != 2:
                continue
            try:
                owns = {face.Tag for face in edge.GetFaces()}
            except Exception:
                continue
            if not (owns & boring):
                continue
            try:
                arc = drf().UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
            except Exception:
                continue
            diameter = round(float(arc.Radius) * 2.0, 3)
            if diameter in seen:
                continue
            seen.add(diameter)
            rows.append((diameter, arc_axis(part, arc, edge)))
    return rows


def arc_axis(part, arc, edge=None):
    """The direction a bore runs: the normal of the plane its circle lies in.

    NOT from `arc.MatrixTag`.  That tag does not resolve to an object at all --
    `part.FindObject(str(tag))` raises "No object found with this name" -- so the old
    implementation silently fell through to its `(0, 0, 1)` default and EVERY circle in
    every part was reported as facing +Z.  The damage stayed invisible until a part whose
    bores run along Y: the hand crank's five circles all looked like Z bores, minimal_views
    believed a single Top view could show them, and the part came out with one narrow view
    at 3:1 and no way to dimension its length.

    Computed from geometry alone, because that is all this NX build offers: AskArcData
    hands back only ArcCenter / Radius / StartAngle / EndAngle / MatrixTag, and neither
    `Modl.AskFaceData` nor `Face.GetFaceData` exists here.

    A full circle's two vertices coincide, so the vertex gives a POINT ON the circle and
    the centre gives the circle's centre: their difference is a radius vector lying in the
    circle's plane.  The start angle says where the arc begins, so a second, perpendicular
    radius follows by rotating that one -- and the cross product of the two radii is the
    plane normal, i.e. the bore axis.  No matrix involved, so a broken tag cannot lie.
    """
    import math as _math
    centre = (float(arc.ArcCenter[0]), float(arc.ArcCenter[1]), float(arc.ArcCenter[2]))
    radius = float(arc.Radius)

    # ---- a radius vector, from a point that is really on the circle.
    radial = None
    if edge is not None:
        try:
            vertices = list(edge.GetVertices())
            if len(vertices) == 2:
                point = vertex_point(vertices[0])
                delta = (point[0] - centre[0], point[1] - centre[1], point[2] - centre[2])
                length = (delta[0] ** 2 + delta[1] ** 2 + delta[2] ** 2) ** 0.5
                # The vertex must actually sit on the circle; a coincident pair on a
                # full circle gives exactly the radius.
                if length > 1e-9 and abs(length - radius) < max(1e-3, radius * 0.02):
                    radial = (delta[0] / length, delta[1] / length, delta[2] / length)
        except Exception:
            radial = None
    if radial is None:
        return (0.0, 0.0, 1.0)

    # ---- a second radius, perpendicular to the first, in the same plane.  Rotating by
    # the arc's own sweep would be ideal but the sweep of a full circle is 360 degrees;
    # instead the plane normal is found from the one radius we trust plus the fact that
    # the circle's plane also contains the axis through the arc's start/end.
    #
    # The practical route: two DIFFERENT circles of the same bore (the two rims of a
    # through hole) share an axis, and their centres give it exactly.  When only one
    # circle is available the radius vector alone is not enough, so fall back to the
    # most-common axis convention -- but mark it as uncertain rather than silently
    # pretending it is Z.
    return radial_axis(part, centre, radius, radial, arc)


def radial_axis(part, centre, radius, radial, arc):
    """The bore axis, from a radius vector plus the circles that share this bore.

    A through hole contributes two rims; the line between their centres IS the axis, and
    it needs no matrix and no face query.  That is the reliable path and it is tried first.
    """
    candidates = []
    try:
        import NXOpen.UF as UFLIB
        ufs = UFLIB.UFSession.GetUFSession()
        for body in list(part.Bodies):
            for other in body.GetEdges():
                if edge_class(other) != 2:
                    continue
                try:
                    vertices = list(other.GetVertices())
                    if len(vertices) != 2:
                        continue
                    a = vertex_point(vertices[0])
                    b = vertex_point(vertices[1])
                    if (abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6
                            or abs(a[2] - b[2]) > 1e-6):
                        continue
                    other_arc = ufs.Curve.AskArcData(other.Tag)
                except Exception:
                    continue
                if abs(float(other_arc.Radius) - radius) > 1e-3:
                    continue
                other_centre = (float(other_arc.ArcCenter[0]),
                                float(other_arc.ArcCenter[1]),
                                float(other_arc.ArcCenter[2]))
                delta = (other_centre[0] - centre[0], other_centre[1] - centre[1],
                         other_centre[2] - centre[2])
                length = (delta[0] ** 2 + delta[1] ** 2 + delta[2] ** 2) ** 0.5
                if length > 1e-6:
                    candidates.append((delta[0] / length, delta[1] / length,
                                       delta[2] / length))
    except Exception:
        candidates = []
    if candidates:
        # Several rims of the same bore all point the same way; average for stability.
        axis = (sum(c[0] for c in candidates) / len(candidates),
                sum(c[1] for c in candidates) / len(candidates),
                sum(c[2] for c in candidates) / len(candidates))
        length = (axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2) ** 0.5
        if length > 1e-9:
            return (axis[0] / length, axis[1] / length, axis[2] / length)
    # ---- last resort: the radius is perpendicular to the axis, so pick the axis that is
    # perpendicular to the radius and closest to a principal direction.  Reported as an
    # approximation by construction, but it is right for the common case of a bore drilled
    # along a principal axis.
    best = None
    for index in range(3):
        principal = [0.0, 0.0, 0.0]
        principal[index] = 1.0
        dot = abs(sum(radial[i] * principal[i] for i in range(3)))
        if best is None or dot < best[0]:
            best = (dot, tuple(principal))
    return best[1]


def cmd_drawing(args):
    part = display_part()
    # Assemblies open lightweight: without loading them the fallback layout has no bounding
    # box to fit the views to (and the views themselves come out half empty).
    try:
        part.LoadFully()
    except Exception:
        pass
    # Part drawings default to A4, assembly drawings to A3: a single part carries three
    # views and a section, an assembly needs room for the parts list above the title block.
    try:
        is_assembly = len(list(part.Bodies)) == 0
    except Exception:
        is_assembly = False
    size = str(args.get("size") or ("A3" if is_assembly else "A4")).upper()
    projection = str(args.get("projection") or "first").lower()
    auto_scale = args.get("autoScale", True)
    use_template = args.get("template", True)
    # "Give me a drawing" must not stack a second empty sheet on a part that already has
    # one: the panel button would otherwise leave a blank current sheet behind and the next
    # PDF print would hand back a white page.
    reuse = args.get("reuse", True)
    scale = args.get("scale") or [1.0, 1.0]
    # Sheet scale (the `scale` pair above) and view scale are different things: the pair is
    # what InsertSheet stores, this one is the ratio the views are drawn at.
    view_ratio = args.get("viewScale")
    if view_ratio is None:
        view_ratio = args.get("view_scale")
    layout_hint = None
    if view_ratio is not None:
        view_ratio = ratio_value(view_ratio)
        # The wizard's auto-scale owns the ratio, so an explicit one has to use our layout.
        layout_hint = "fitted"

    empty_sheet = None
    sheet = None
    if reuse:
        paper = SHEET_SIZES.get(size)
        for existing in list(part.DraftingDrawingSheets):
            try:
                populated = [v for v in existing.GetDraftingViews()
                             if (view_summary(v)["visible"] or 0) > 0]
            except Exception:
                populated = []
            if not populated:
                # A brand-new part already carries one blank sheet; drawing into it keeps
                # the part at a single sheet instead of leaving an empty page behind.
                if empty_sheet is None:
                    try:
                        if paper is None or (abs(float(existing.Length) - paper[0]) < 1.0
                                             and abs(float(existing.Height) - paper[1]) < 1.0):
                            empty_sheet = existing
                    except Exception:
                        empty_sheet = existing
                continue
            # Only reuse a sheet of the size that was asked for: picking A2 after an A3
            # sheet exists otherwise silently keeps printing on the small sheet.
            if paper is not None:
                try:
                    same = (abs(float(existing.Length) - paper[0]) < 1.0
                            and abs(float(existing.Height) - paper[1]) < 1.0)
                except Exception:
                    same = True
                if not same:
                    continue
            # refresh: keep the sheet but re-run the rules on it (re-rule an old drawing)
            if args.get("refresh"):
                sheet = existing
                out("refreshing sheet:", fix_text(existing.Name), len(populated), "view(s)")
                break
            out("reusing sheet:", fix_text(existing.Name), len(populated), "view(s)")
            return {"sheet": sheet_info(existing), "sheets": sheet_list(part),
                    "reused": True,
                    "views": [view_summary(v) for v in populated]}

    angle = NX.Drawings.DrawingSheetProjectionAngleType.ThirdAngle if projection.startswith("t") \
        else NX.Drawings.DrawingSheetProjectionAngleType.FirstAngle
    if sheet is None:
        sheet = empty_sheet
    if sheet is not None:
        try:
            sheet.ActivateForSketching()
            out("drawing into the existing sheet:", fix_text(sheet.Name))
        except Exception as error:
            out("could not activate the empty sheet:", repr(error))
            sheet = None
    if sheet is None and use_template:
        tpl = os.path.join(s.GetEnvironmentVariableValue("UGII_BASE_DIR"),
                           "DRAFTING", "templates", "Drawing-%s-Size-template.prt" % size)
        if os.path.exists(tpl):
            known = {sh.Tag for sh in list(part.DraftingDrawingSheets)}
            # Create() is a void method: the new sheet has to be found afterwards.
            part.DrawingSheets.SheetTemplates.Create(tpl)
            fresh = [sh for sh in list(part.DraftingDrawingSheets) if sh.Tag not in known]
            if fresh:
                sheet = fresh[0]
                out("template sheet:", fix_text(sheet.Name))
            else:
                out("template produced no sheet, falling back to InsertSheet")
    if sheet is None:
        sheet_size = getattr(NX.Drawings.DrawingSheetStandardSheetSize, size)
        name = str(args.get("sheetName") or "SH1")
        # A second call with the same name used to abort the whole drawing; bump instead.
        taken = {fix_text(sh.Name).lower() for sh in list(part.DraftingDrawingSheets)}
        if name.lower() in taken:
            # Suffix rather than increment: "SH_A3" carries the paper size, so turning it
            # into "SH_A4" would rename the page instead of de-duplicating it.
            index = 2
            while ("%s_%d" % (name, index)).lower() in taken:
                index += 1
            name = "%s_%d" % (name, index)
            out("sheet name taken, using:", name)
        sheet = part.DraftingDrawingSheets.InsertSheet(
            name, sheet_size, float(scale[0]), float(scale[1]), angle)
        out("inserted sheet:", repr(sheet))

    # The wizard's "auto scale" is not trustworthy on big parts: a 500 mm fixture comes out at
    # 0.83 on an A3 and the outer views hang off the paper, and views cannot be deleted again.
    # So the default is our own layout, computed from the part's bounding box; the wizard is
    # only used when the caller asks for it (layout: "wizard").
    layout = layout_hint or str(args.get("layout") or "fitted").lower()
    created = []
    plan_ok = False
    if sheet is not None and args.get("refresh"):
        # Re-ruling an existing drawing: keep the views that are already there instead of
        # dropping a second set on top of them.
        created = views_on_sheet(part, sheet)
        plan_ok = bool(created)
        out("refresh: keeping", len(created), "existing view(s)")
    if layout == "wizard":
        wiz = part.DraftingViews.CreateViewCreationWizardBuilder()
        # The drawing layout part must be named explicitly, otherwise Commit
        # raises "Drawing part file and drawing layout part file differ."
        wiz.Part = part
        wiz.BaseView = str(args.get("baseView") or "TOP")
        flags = args.get("views") or {"front": True, "right": True}
        for key, prop in (("top", "TopView"), ("front", "FrontView"), ("right", "RightView"),
                          ("left", "LeftView"), ("back", "BackView"), ("bottom", "BottomView"),
                          ("isometric", "IsometricView"), ("trimetric", "TrimetricView")):
            if key in flags:
                setattr(wiz, prop, bool(flags[key]))
        wiz.AutoScale = bool(auto_scale)
        plan_ok = True
        try:
            wiz.Commit()
        except Exception as e:
            # A wizard that refuses (no model views, assembly quirks) must not leave behind a
            # bare sheet: fall through to explicit base views below.
            out("view wizard failed:", repr(e))
            plan_ok = False
        try:
            created = views_on_sheet(part, sheet)
        except Exception:
            created = []
    if not created or not plan_ok:
        # Explicit fallback: one base view per orientation, laid out in first-angle order.
        # The view scale has to be derived from the part, otherwise a 500 mm fixture is drawn
        # at 1:1 and runs off an A3 sheet (the panel's "print PDF" then hands back a page that
        # is mostly missing).
        out("falling back to explicit base views")
        width = height = 0.0
        try:
            extent = []
            if len(list(part.Bodies)) > 0:
                extent = measure(part).get("bboxSize") or []
            elif getattr(part, "ComponentAssembly", None) is not None:
                shown = component_bodies_in(part)
                if not shown:
                    # Lightweight components carry no bodies until the part is loaded.
                    try:
                        part.LoadFully()
                    except Exception:
                        pass
                    shown = component_bodies_in(part)
                if shown:
                    box = s.Measurement.GetBoundingBoxProperties(
                        shown, 0, NX.Point3d(0.0, 0.0, 0.0), False)
                    extent = list(box[2])
            if len(extent) >= 2:
                width = float(extent[0])
                height = float(extent[1])
        except Exception as e:
            out("fallback fit: no bounding box", repr(e))
        paper = SHEET_SIZES.get(str(size).upper(), (420.0, 297.0))
        if width > 0 and height > 0:
            # The fit has to use the part's LARGEST extent, not just X and Y: a long shaft
            # lying along Z (the hand crank is 8 x 56 x 8) has its length in the third
            # dimension, and fitting on X/Y alone blew it up to 3:1 until the drawing ran
            # off the frame.  How much of the sheet one view may take depends on how many
            # views there will be, which is not known yet -- so the conservative case is
            # assumed here (two views sharing the sheet) and layout_views_with_rules() then
            # scales up when there is room.
            extent = []
            try:
                box = measure(part)
                extent = box.get("bboxSize") or []
            except Exception:
                extent = []
            longest = max([float(v) for v in extent] or [width, height])
            if longest > 0:
                # Choose the ratio so the part occupies a sensible share of the paper, not
                # the largest share the frame allows.  Two views plus their dimension
                # corridors have to fit, and a reading needs ~20 mm of corridor on each
                # side, so a part is drawn at about a third of the sheet's height per view.
                # The old 0.72/0.40 factors produced 2:1, 3:1 and 6:1 sheets whose
                # dimensions ran into each other and off the paper.
                fit = min((paper[0] * 0.42) / longest, (paper[1] * 0.26) / longest)
            else:
                fit = min((paper[0] * 0.42) / width, (paper[1] * 0.26) / height)
        else:
            # Extent unknown (assembly whose components never loaded): pick a scale that is
            # small enough for the usual fixture-sized part rather than filling the sheet.
            fit = 0.4
        ratio = 1.0
        # 1.0 -> 0.5 used to be a single step, which is how a part ended up at 0.334 on a
        # sheet that had room for 0.75: the table now carries the half and quarter steps.
        for candidate in (2.0, 1.5, 1.0, 0.75, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1,
                          0.05, 0.02, 0.01):
            if candidate <= fit:
                ratio = candidate
                break
        else:
            ratio = 0.01
        if view_ratio is not None:
            ratio = view_ratio
            out("view scale pinned to %g by the caller" % ratio)
        out("initial view scale %g:1 (part %gx%g mm on %s)"
            % (ratio, width, height, size))
        model_views = {}
        try:
            for mv in list(part.ModelingViews):
                model_views[fix_text(mv.Name).upper()] = mv
        except Exception:
            model_views = {}
        grid = {"FRONT": (0.30, 0.66), "TOP": (0.30, 0.30),
                "RIGHT": (0.71, 0.68), "ISOMETRIC": (0.74, 0.29),
                "LEFT": (0.72, 0.36), "BACK": (0.30, 0.48),
                "BOTTOM": (0.52, 0.12), "TRIMETRIC": (0.72, 0.12)}
        # Only the views the caller named; the old build ignored `views` here and always
        # produced the same four.
        flags = args.get("views") or {}
        section_will_replace = False
        order = [("front", "FRONT"), ("top", "TOP"), ("right", "RIGHT"), ("left", "LEFT"),
                 ("back", "BACK"), ("bottom", "BOTTOM"), ("isometric", "ISOMETRIC"),
                 ("trimetric", "TRIMETRIC")]
        if flags:
            wanted = [(name, slot) for slot, (key, name) in enumerate(order) if flags.get(key)]
            if not wanted:
                raise RuntimeError("views was given but no known name was set true (known: %s)"
                                   % ", ".join(k for k, _ in order))
        else:
            # No isometric unless the caller asks for it: it eats the largest block on the
            # sheet without carrying a single dimension.  Which orthographic views remain
            # is decided by size coverage -- see minimal_views(): Right is dropped when it
            # repeats what Top and Front already show.
            picked = minimal_views(part)
            if picked:
                wanted = [(name, index) for index, name in enumerate(picked)]
                out("minimal view set:", ", ".join(picked))
            else:
                wanted = [("FRONT", 0), ("TOP", 1)]
        # A part with an internal cavity is drawn cut open, and the section takes the
        # place of the plain elevation.  This has to happen HERE, before any view exists:
        # NX has no working delete-view call in this session, so a view created now cannot
        # be taken off the sheet afterwards.
        if (len(list(part.Bodies)) > 0
                and str(args.get("section") if args.get("section") is not None
                        else "replace").lower() not in ("false", "0", "no", "off")):
            try:
                if has_internal_features(part) and not bores_already_visible(part, sheet):
                    # The section will only be created when the bore is invisible in every
                    # view on the sheet.  Dropping FRONT here on a weaker test (the sheet was
                    # empty, so the test said "not visible") left parts with a single view:
                    # FRONT gone, section refused, and the length had nowhere to live.
                    planned = [name for name, _ in wanted if name != "FRONT"]
                    if planned and bores_visible_in_model_views(part, planned):
                        out("section would only repeat the bores: keeping FRONT")
                    else:
                        kept = [item for item in wanted if item[0] != "FRONT"]
                        if kept and len(kept) < len(wanted):
                            wanted = kept
                            section_will_replace = True
                            out("section replaces FRONT: creating %s only"
                                % ", ".join(name for name, _ in wanted))
            except Exception as error:
                out("section pre-check failed:", repr(error))
        placed = []
        for index, (name, slot) in enumerate(wanted):
            mv = model_views.get(name)
            if mv is None:
                continue
            builder = part.DraftingViews.CreateBaseViewBuilder(None)
            try:
                builder.SelectModelView.SelectedView = mv
                builder.Scale.ScaleType = NX.Drawings.ViewScaleBuilderType.Ratio
                # An isometric view looks bigger than its plan on paper; keep it inside its
                # quadrant instead of letting it run over the plan view. An explicit ratio
                # from the caller is used as given.
                shrink = 0.78 if (name == "ISOMETRIC" and view_ratio is None) else 1.0
                builder.Scale.Numerator = float(ratio * shrink)
                builder.Scale.Denominator = 1.0
                spot = grid.get(name, (0.30, 0.66))
                builder.Placement.Placement.Value = part.Points.CreatePoint(
                    NX.Point3d(paper[0] * spot[0], paper[1] * spot[1], 0.0))
                builder.Validate()
                builder.Commit()
                placed.append(name)
                # The builder's Numerator/Denominator do NOT come back as the drawn ratio:
                # asking for 1.0 produced a view that reports 2.0 through AskViewScale,
                # which is why every batch sheet came out at twice the scale it asked for
                # (2:1, 3:1, 6:1 on parts that fit at 1:1).  The ratio is therefore set
                # again through UF_DRAW_set_view_scale, the call that actually drives what
                # NX draws, and the result is read back to confirm it.
                try:
                    wanted_ratio = float(ratio * shrink)
                    fresh_views = [v for v in drafting_views(part)
                                   if fix_text(v.Name).split("@")[0].upper() == name]
                    for fresh_view in fresh_views:
                        uf_session().Draw.SetViewScale(fresh_view.Tag, wanted_ratio)
                        actual = true_scale(fresh_view)
                        if abs(actual - wanted_ratio) > 0.01:
                            out("view %s scale asked %.3f, got %.3f"
                                % (name, wanted_ratio, actual))
                except Exception as error:
                    out("base view scale not applied:", name, repr(error))
            except Exception as e:
                out("base view failed:", name, repr(e))
        created = views_on_sheet(part, sheet)
        out("explicit base views:", ", ".join(placed))

    layout_report = None
    section_report = None
    frame_report = None
    notes = []
    layout_mode = str(args.get("layout") or "").lower()

    # A part with an internal cavity gets a section through its middle.  Whether that
    # section REPLACES the plain elevation is decided before any view is created, because
    # NX offers no working way to delete a drafting view in this session: UF_DRAW has no
    # DeleteView, and both Obj.DeleteObject and DraftingViews.DeleteViewsInOriginalPart
    # return without touching the collection.  "Create then remove" therefore leaves both
    # views on the sheet, so the decision has to come first.
    section_mode = str(args.get("section") if args.get("section") is not None else "replace").lower()
    section_enabled = section_mode not in ("false", "0", "no", "off")
    if created and len(list(part.Bodies)) > 0 and section_enabled:
        try:
            section_report = ensure_section_view(part, sheet)
            if section_report.get("created"):
                created = views_on_sheet(part, sheet)
            else:
                notes.append("section: %s" % section_report.get("reason"))
        except Exception as error:
            notes.append("section: %r" % (error,))
            out("auto section skipped:", repr(error))

    # The views fill at least 40 % of the sheet with gaps of at least 10 % of its width.
    if created and layout_mode not in ("none", "keep"):
        try:
            reserve = args.get("reserveTitle")
            layout_report = layout_views_with_rules(
                sheet, created,
                reserve=True if reserve is None else bool(reserve),
                min_area=args.get("minViewArea"),
                gap_ratio=args.get("viewGap"),
                pair_layout=args.get("pairLayout") or "horizontal")
            created = views_on_sheet(part, sheet)
            if layout_report and not layout_report.get("met"):
                notes.append(
                    "layout: views cover %.1f %% of the sheet, %.0f %% required "
                    "(%.0f mm gaps on %gx%g leave no room; a larger sheet or fewer views "
                    "would be needed)"
                    % (100.0 * layout_report.get("areaRatio", 0.0),
                       100.0 * layout_report.get("target", LAYOUT_MIN_AREA),
                       layout_report.get("gap", 0.0),
                       float(sheet.Length), float(sheet.Height)))
        except Exception as e:
            notes.append("layout: %r" % (e,))
            out("auto layout skipped:", repr(e))

    # ---- the sheet must not print the model's datums.
    #
    # A drafting view projects whatever the model shows, and that includes the absolute
    # coordinate system and the datum axes and planes every part carries.  They print as
    # long thin crosses through the middle of each view -- visible on the panel folder's
    # 阀体 sheet as a hairline cross over every view -- and they are not part of the part.
    # Blanking them (the same switch as Hide in NX) removes them from the printed sheet
    # while leaving the geometry untouched.
    if created:
        try:
            hidden = hide_datums(sheet)
            if hidden.get("count"):
                notes.append("hidden %d datum object(s)" % hidden["count"])
        except Exception as error:
            notes.append("datums: %r" % (error,))
            out("datum hiding skipped:", repr(error))

    # Every sheet carries a real GB frame and title block in the bottom-right corner; the
    # frame a NX template carries does not print, so it is drawn as sheet curves here.
    if args.get("frame", True) is not False and sheet is not None:
        scale_text = None
        try:
            if created:
                drawn_ratio = true_scale(created[0])
                if drawn_ratio >= 1:
                    scale_text = "%g:1" % round(drawn_ratio, 2)
                else:
                    scale_text = "1:%g" % round(1.0 / drawn_ratio, 2)
        except Exception:
            scale_text = None
        try:
            frame_report = cmd_drawing_frame({
                "partName": args.get("partName"),
                "drawingNo": args.get("drawingNo"),
                "material": args.get("material"),
                "designer": args.get("designer"),
                "checker": args.get("checker"),
                "company": args.get("company"),
                "scale": scale_text,
            })
        except Exception as error:
            notes.append("frame: %r" % (error,))
            out("frame skipped:", repr(error))

    info = {"sheet": sheet_info(current_sheet(part)),
            "sheets": sheet_list(part),
            "reused": False,
            "views": [view_summary(v) for v in created],
            "layout": layout_report,
            "section": section_report,
            "frame": frame_report,
            "notes": notes}
    if notes:
        # A drawing that could not follow the rules has to say so somewhere a caller can
        # read without opening the log of the NX process.
        try:
            with open(os.path.join(OUT_DIR, "drawing_notes.log"), "a",
                      encoding="utf-8") as handle:
                handle.write("%s  %s  ::  %s\n"
                             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                fix_text(part.Leaf), " | ".join(notes)))
        except Exception:
            pass
    return info


def cmd_pdf(args):
    part = display_part()
    target = args["path"]
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    sheets = []
    wanted = args.get("sheets")
    if wanted:
        names = {str(n) for n in wanted}
        for sh in list(part.DraftingDrawingSheets):
            if fix_text(sh.Name) in names:
                sheets.append(sh)
    if not sheets:
        # Print something that has geometry on it: the current sheet may well be a bare one
        # left over from an earlier "new drawing" click, and a PDF of it is a white page.
        best = None
        best_count = 0
        try:
            current = current_sheet(part)
        except Exception:
            current = None
        for sh in list(part.DraftingDrawingSheets):
            try:
                count = len([v for v in sh.GetDraftingViews() if (view_summary(v)["visible"] or 0) > 0])
            except Exception:
                count = 0
            if current is not None and sh.Tag == current.Tag and count > 0:
                best, best_count = sh, count
                break
            if count > best_count:
                best, best_count = sh, count
        if best is not None:
            sheets = [best]
            out("printing sheet:", fix_text(best.Name), best_count, "view(s)")
    if not sheets:
        # Nothing with geometry anywhere: build a sheet here, so "打印 PDF" works right
        # after opening a part, then insist on a real sheet before touching its Name.
        try:
            cmd_drawing({"size": args.get("size") or "A3", "projection": "first",
                         "views": {"top": True, "front": True, "right": True}})
        except Exception:
            pass
        sheets = [sh for sh in list(part.DraftingDrawingSheets)
                  if len([v for v in sh.GetDraftingViews()
                          if (view_summary(v)["visible"] or 0) > 0]) > 0]
    if not sheets:
        try:
            current = current_sheet(part)
        except Exception:
            current = None
        if current is None:
            raise RuntimeError("this part has no drawing sheet yet - build one first "
                               "(nx_drawing, or the panel's 建图纸 button)")
        sheets = [current]
    for candidate in sheets:
        # A sheet that is not on screen does not get its CGM exported: open it first.
        try:
            candidate.Open()
        except Exception:
            pass
    builder = part.PlotManager.CreatePrintPdfbuilder()
    # Printing nothing produces an empty file: the source sheets must be set.
    builder.SourceBuilder.SetSheets(sheets)
    builder.Filename = target
    builder.Action = NX.PrintPDFBuilderActionOption.New
    try:
        builder.OutputText = NX.PrintPDFBuilderOutputTextOption.Text
    except Exception:
        pass
    builder.Commit()
    ok = os.path.exists(target)
    return {
        "path": target,
        "exists": ok,
        "bytes": os.path.getsize(target) if ok else 0,
        "sheets": [fix_text(sh.Name) for sh in sheets],
        "sheet": fix_text(sheets[0].Name) if sheets else None,
        "views": len([v for v in sheets[0].GetDraftingViews() if (view_summary(v)["visible"] or 0) > 0])
        if sheets else 0,
    }


# ---------------------------------------------------------------- drafting extras
def drafting_views(part):
    try:
        return list(current_sheet(part).GetDraftingViews())
    except Exception:
        return list(part.DraftingViews)


def pick_view(part, wanted):
    views = drafting_views(part)
    if not views:
        raise RuntimeError("the part has no drafting views yet — run nx_drawing first")
    names = ", ".join(fix_text(v.Name) for v in views)
    if wanted:
        key = str(wanted).strip().lower()
        for v in views:
            if fix_text(v.Name).lower() == key:
                return v
        for v in views:
            if key in fix_text(v.Name).lower():
                return v
        raise RuntimeError("no view matches %r (views: %s)" % (wanted, names))
    for v in views:
        name = fix_text(v.Name).lower()
        if type(v).__name__ == "BaseView" and ("top" in name or "front" in name):
            return v
    return views[0]


def view_frame(view):
    # GetDrawingReferencePoint is the view centre on the sheet, Origin is the model
    # point that sits there mirrored, so model -> sheet is ref + (p + Origin) * scale.
    ref = view.GetDrawingReferencePoint()
    try:
        ox, oy = float(view.Origin.X), float(view.Origin.Y)
    except Exception:
        ox = oy = 0.0
    return float(ref.X), float(ref.Y), true_scale(view), ox, oy


def sheet_of(view, x, y):
    rx, ry, scale, ox, oy = view_frame(view)
    return rx + (float(x) + ox) * scale, ry + (float(y) + oy) * scale


def boxes_overlap(a, b):
    """a, b: (x0, y0, x1, y1) rectangles on the sheet."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def view_need(view, part=None, fallback=(130.0, 90.0)):
    """Rough sheet-space size (w, h) of a view, used to keep a new frame on the paper.

    A drawing view in this batch session exposes no BoundingBox, so the size comes from the
    part's own extents scaled by the view ratio -- and the ratio has to be the real one from
    UF, not view.Scale (which reads ~0.4 on a 1:1 view and made every frame look half size).
    """
    try:
        scale = abs(float(true_scale(view))) or 1.0
    except Exception:
        scale = 1.0
    size = fallback
    if part is not None:
        try:
            box = measure(part)
            lo = box.get("bboxMin") or [0.0, 0.0, 0.0]
            hi = box.get("bboxMax") or [0.0, 0.0, 0.0]
            width = abs(float(hi[0]) - float(lo[0]))
            height = abs(float(hi[1]) - float(lo[1]))
            if width > 0 and height > 0:
                size = (width, height)
        except Exception:
            size = fallback
    return (size[0] * scale, size[1] * scale)


def free_spot(part, need=None, avoid_title_block=True):
    """A sheet point where a view of about `need` mm still fits on the paper.

    Choosing the point farthest from every view centre put a section view hard against
    the right edge, so its frame ran past the printable area and the view printed empty.
    Now the candidate frame has to stay inside the margins, clear of the GB title block
    and off every existing view, with a bias towards the upper right.
    """
    sheet = current_sheet(part)
    width, height = float(sheet.Length), float(sheet.Height)
    need = need or (130.0, 90.0)
    try:
        half_w = max(50.0, float(need[0]) * 0.5)
        half_h = max(38.0, float(need[1]) * 0.5)
    except Exception:
        half_w, half_h = 65.0, 45.0
    left, other, bottom, top = 25.0, 10.0, 10.0, 10.0
    lo_x, hi_x = left + half_w, width - other - half_w
    lo_y, hi_y = bottom + half_h, height - top - half_h
    if hi_x <= lo_x:
        lo_x = hi_x = width * 0.5
    if hi_y <= lo_y:
        lo_y = hi_y = height * 0.5
    taken = []
    for v in drafting_views(part):
        try:
            r = v.GetDrawingReferencePoint()
            taken.append((float(r.X), float(r.Y)))
        except Exception:
            pass
    title_box = None
    if avoid_title_block:
        # GB title block sits on the bottom right: 180 x 56 mm.
        title_box = (width - other - 180.0, bottom, width - other, bottom + 56.0)

    def blocked(x, y):
        box = (x - half_w, y - half_h, x + half_w, y + half_h)
        if title_box is not None and boxes_overlap(box, title_box):
            return True
        for tx, ty in taken:
            if boxes_overlap(box, (tx - half_w, ty - half_h, tx + half_w, ty + half_h)):
                return True
        return False

    candidates = []
    y = lo_y
    while y <= hi_y + 1e-9:
        x = lo_x
        while x <= hi_x + 1e-9:
            if not blocked(x, y):
                distance = min([((x - tx) ** 2 + (y - ty) ** 2) ** 0.5
                                for tx, ty in taken] or [9.9e3])
                score = distance + 0.35 * (x - lo_x) + 0.5 * (y - lo_y)
                candidates.append((score, x, y))
            x += 10.0
        y += 10.0
    if not candidates:
        return min(max(width * 0.55, lo_x), hi_x), min(max(height * 0.55, lo_y), hi_y)
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def view_summary(view):
    """What a drafting view carries.  `visible` is None when NX refuses to answer.

    AskVisibleObjects raises "The view is not displayed" on a sheet that has never been
    displayed in this session -- which happens on a part opened straight from disk that
    carries a stale sheet beside a fresh one.  Reporting None keeps the rest of the view
    information available instead of taking the whole command down.
    """
    try:
        visible = len(list(view.AskVisibleObjects()))
    except Exception:
        visible = None
    return {
        "name": fix_text(view.Name),
        "kind": type(view).__name__,
        # view.Scale is an internal ratio (0.83 on a 1:1 view); the real one comes from UF.
        "scale": round(true_scale(view), 4),
        "internalScale": round(float(view.Scale), 4),
        "sheetPoint": [round(float(view.GetDrawingReferencePoint().X), 2),
                       round(float(view.GetDrawingReferencePoint().Y), 2)],
        "visible": visible,
    }


def cmd_section_view(args):
    part = display_part()
    parent = pick_view(part, args.get("parent"))
    start = args.get("start") or args.get("from")
    end = args.get("end") or args.get("to")
    if not start or not end:
        raise RuntimeError("start and end are the two ends of the cut line in the parent view's "
                           "model space, e.g. start [-10,30,10] and end [110,30,10]")
    kind = str(args.get("type") or "pointtopoint").replace("-", "").replace("_", "").lower()
    types = NX.Drawings.SectionViewBuilderSectionLineType
    mapping = {"pointtopoint": types.PointToPoint, "half": types.Half,
               "pointandangle": types.PointAndAngle, "revolved": types.Revolved,
               "stepped": types.SimpleStepped}
    if kind not in mapping:
        raise RuntimeError("type must be one of %s" % ", ".join(sorted(mapping)))

    at = args.get("at") or free_spot(part, view_need(parent, part))

    def build(candidate):
        known = {v.Tag for v in drafting_views(part)}
        builder = part.DraftingViews.CreateSectionViewBuilder(None)
        # The parent has to be set before the cut segments, otherwise NX rejects both.
        builder.ParentView.View.Value = parent
        builder.SectionViewType = mapping[candidate]
        segments = builder.SectionLineSegments.SegmentLocation
        segments.AddCutSegment(part.Points.CreatePoint(pnt(*start)))
        segments.AddCutSegment(part.Points.CreatePoint(pnt(*end)))
        builder.ViewPlacement.Placement.Value = part.Points.CreatePoint(pnt(at[0], at[1], 0.0))
        builder.Validate()
        builder.Commit()
        fresh = [v for v in drafting_views(part) if v.Tag not in known]
        if not fresh:
            raise RuntimeError("the section view was committed but no new view appeared")
        return fresh[-1]

    # NX refuses a point-to-point section for some cut directions ("Invalid hinge line")
    # while the same two points commit as a stepped section, so retry instead of failing.
    order = [kind]
    if not args.get("strict"):
        order += [c for c in ("stepped", "half") if c != kind]
    attempts = []
    last = None
    for candidate in order:
        try:
            view = build(candidate)
        except Exception as exc:
            last = exc
            text = "%s" % exc
            attempts.append("%s: %s" % (candidate, text))
            if "hinge line" not in text and "associativity" not in text:
                raise
            continue
        result = {"view": view_summary(view), "parent": fix_text(parent.Name),
                  "type": candidate, "at": [round(float(at[0]), 2), round(float(at[1]), 2)],
                  "requested": kind}
        if attempts:
            result["fellBack"] = True
            result["note"] = ("NX rejected %s for this cut line (%s); the same cut went in "
                              "as %s" % (kind, attempts[0].split(": ", 1)[-1], candidate))
        return result
    raise RuntimeError("no section type would take this cut line -- %s" % "; ".join(attempts))


def cmd_detail_view(args):
    part = display_part()
    parent = pick_view(part, args.get("parent"))
    center = args.get("center")
    radius = args.get("radius")
    if not center or radius is None:
        raise RuntimeError("center [x,y] and radius (both in the parent view's model space) are required")
    shape = str(args.get("shape") or "circular").strip().lower()
    ratio = float(args.get("scale") or 2.0)
    cx, cy = sheet_of(parent, center[0], center[1])
    r_sheet = float(radius) * float(parent.Scale)

    known = {v.Tag for v in drafting_views(part)}
    builder = part.DraftingViews.CreateDetailViewBuilder(None)
    builder.Parent.View.Value = parent
    if shape.startswith("rect"):
        builder.Type = NX.Drawings.DetailViewBuilderTypes.RectangleByCenterAndCorner
        second = (cx + r_sheet, cy - r_sheet)
    else:
        builder.Type = NX.Drawings.DetailViewBuilderTypes.Circular
        second = (cx + r_sheet, cy)
    builder.BoundaryPoint1 = part.Points.CreatePoint(pnt(cx, cy, 0.0))
    builder.BoundaryPoint2 = part.Points.CreatePoint(pnt(second[0], second[1], 0.0))
    builder.Scale.ScaleType = NX.Drawings.ViewScaleBuilderType.Ratio
    builder.Scale.Numerator = ratio
    builder.Scale.Denominator = 1.0
    at = args.get("at") or free_spot(part, view_need(parent, part))
    builder.Origin.Placement.Value = part.Points.CreatePoint(pnt(at[0], at[1], 0.0))
    builder.Validate()
    builder.Commit()
    fresh = [v for v in drafting_views(part) if v.Tag not in known]
    if not fresh:
        raise RuntimeError("the detail view was committed but no new view appeared")
    return {"view": view_summary(fresh[-1]), "parent": fix_text(parent.Name),
            "ratio": ratio, "boundary": [[round(cx, 2), round(cy, 2)],
                                         [round(second[0], 2), round(second[1], 2)]],
            "at": [round(float(at[0]), 2), round(float(at[1]), 2)]}

# ---------------------------------------------------------------- view and sheet control
# view.Scale is an internal number (0.834 on a 1:1 view), so every ratio that has to be
# read back or recomputed goes through UF_DRAW_ask_view_scale instead.


# ---------------------------------------------------------------- view and sheet control
# view.Scale is an internal number (0.834 on a 1:1 view), so every ratio that has to be
# read back or recomputed goes through UF_DRAW_ask_view_scale instead.
def uf_session():
    import NXOpen.UF as UFLIB
    return UFLIB.UFSession.GetUFSession()


def ratio_value(value):
    """1:2 / 0.5 / {"num":1,"den":2} -> 0.5"""
    if isinstance(value, dict):
        num = float(value.get("num") or value.get("numerator") or 1.0)
        den = float(value.get("den") or value.get("denominator") or 1.0)
        return num / den if den else 1.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        head, tail = text.split(":", 1)
        num, den = float(head), float(tail)
        return num / den if den else 1.0
    return float(text)


def true_scale(view):
    """The ratio the view is actually DRAWN at, for mapping model space to sheet mm.

    `UF_DRAW_ask_view_scale` does not answer this: it returns the NOMINAL scale (0.5 on
    the panel's assembly sheet) while the view prints at `view.Scale` (0.2950).  Mapping
    with the nominal value inflated every projected feature by a factor of 1.7, which is
    what made the balloon script think 泵体's footprint was 45 mm wide instead of 26.5 and
    push every leader far out to reach it.  `view.Scale` is the number NX draws at, so it
    is the one that belongs in a projection.
    """
    try:
        return float(view.Scale)
    except Exception:
        try:
            return float(uf_session().Draw.AskViewScale(view.Tag)[1])
        except Exception:
            return 1.0

# Standard slots for the named modelling views. Clustering the views by where the builder
# happened to drop them looked reasonable but mis-grouped rows whose centres differed by a
# few millimetres, so the slot comes from the view name instead.
VIEW_SLOTS = {"FRONT": (0, 0), "TOP": (0, 1), "BOTTOM": (0, 2),
              "RIGHT": (1, 0), "BACK": (2, 0), "LEFT": (1, 1),
              "ISOMETRIC": (2, 0), "TRIMETRIC": (2, 1), "DIMETRIC": (2, 1)}

# Drawing rules: every gap between views is at least 10 % of the sheet width, the block is
# spread evenly inside the frame, and the views are blown up as far as the sheet allows
# (min_area defaults to 0, so the search simply takes the largest scale that still fits).
LAYOUT_MIN_AREA = 0.0
LAYOUT_GAP_RATIO = 0.10
# Ratios tried from large to small; the first one that still fits AND reaches the area
# target wins, so a small part is blown up instead of leaving three quarters empty.
SCALE_LADDER = (5.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.25, 1.0, 0.8, 0.75, 0.6, 0.5,
                0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0.02)
LAYOUT_ORDER = ("FRONT", "TOP", "RIGHT", "LEFT", "BACK", "BOTTOM",
                "ISOMETRIC", "TRIMETRIC", "DIMETRIC")


def sheet_margins(width, height):
    """GB frame margins: 25 mm binding edge on the left (20 small), 10 mm (5 small) elsewhere."""
    large = width > 500.0
    return (25.0 if large else 20.0), (10.0 if large else 5.0)


def view_boxes(views):
    """Sheet-space boxes of the views, straight from NX."""
    uf = uf_session()
    boxes = {}
    for view in views:
        try:
            b = [float(v) for v in uf.Draw.AskViewBorders(view.Tag)]
            boxes[view.Name] = (min(b[0], b[2]), min(b[1], b[3]),
                                max(b[0], b[2]), max(b[1], b[3]))
        except Exception:
            boxes[view.Name] = None
    return boxes


def order_views(views):
    """Projection order first, then sections, then details, then anything else."""
    def rank(view):
        head = str(view.Name).split("@")[0].upper()
        if head in LAYOUT_ORDER:
            return (0, LAYOUT_ORDER.index(head), head)
        if head.startswith("SX"):
            return (1, 0, head)
        if head.startswith("DETAIL"):
            return (2, 0, head)
        return (3, 0, head)
    return sorted(views, key=rank)


def projection_alignment(name):
    """Where a view sits relative to the front view, in first-angle projection.

    GB drawings are first angle, so the drawing is read by projection rather than by a
    grid: the top view goes BELOW the front view, a side view goes to its RIGHT, and each
    one shares the front view's other centre line.  Splitting the sheet into equal cells
    ignores that, which is why the views used to sit offset by whatever a cell happened to
    be wide.

    A SECTION view is the elevation here: when the cut takes FRONT's place it becomes the
    anchor (see aligned_positions), and the others project from it exactly as they would
    from a plain front view -- Top below, Right beside.  Without this the section was
    treated as a free view and got parked in a corner.

    Returns ("x", -1) for a view stacked below, ("x", 1) for one above, ("y", +1) for one
    to the right, ("y", -1) for one to the left, or None when the view is not tied to the
    front view (isometric, section, detail).
    """
    head = str(name).split("@")[0].upper()
    if head == "TOP":
        return ("x", -1)       # first angle: plan below the elevation
    if head == "BOTTOM":
        return ("x", 1)
    if head == "RIGHT":
        return ("y", 1)        # first angle: the left view is drawn on the right
    if head == "LEFT":
        return ("y", -1)
    if head == "BACK":
        return ("y", 2)
    return None


def aligned_positions(entries, sheet_w, sheet_h, gap, reserve_title, order,
                      pair_layout="horizontal"):
    """Place views on projection axes instead of a grid.

    The front view is the anchor.  Every other orthographic view is locked to it on one
    axis -- Top/Bottom share its X, Right/Left share its Y -- and is pushed far enough
    out on the other axis that the borders clear each other by `gap`.  Free views
    (isometric, sections) go into the space that is left over.

    The anchor is not pinned to the middle of the sheet: the whole chain (Top above,
    Front in the middle, Bottom below) is measured first and then centred as a block,
    otherwise a tall stack runs off the top edge and the caller silently falls back to
    the grid.

    `entries` is [(name, width, height)] and `order` is the placement order; the return
    value matches plan_grid: (positions, feasible, view_area, block).
    """
    by_name = {name: (width, height) for name, width, height in entries}
    names = [name for name, _, _ in entries]
    anchor = None
    # A section that replaced the front view IS the elevation: the cut is drawn in the
    # elevation's place, so it is the anchor the others project from.  Looking only for
    # FRONT/TOP/RIGHT left a sectioned part anchored on Top and the section dropped into a
    # spare corner -- the pump body came out with the cut in the top-left corner, Top
    # below it and Right beside it, which is not a projection at all.
    for candidate in ("FRONT", "SX", "TOP", "RIGHT"):
        for name in names:
            if str(name).split("@")[0].upper() == candidate:
                anchor = name
                break
        if anchor:
            break
    if anchor is None:
        anchor = names[0]
    binding, margin = sheet_margins(sheet_w, sheet_h)
    usable_w = sheet_w - binding - margin
    inset = max(6.0, 0.045 * usable_w)
    x_lo, x_hi = binding + inset, sheet_w - margin - inset
    y_lo, y_hi = margin + inset, sheet_h - margin - inset
    # ---- room for the dimensions, not just for the views.
    #
    # The scale ladder used to grow a view until its BORDER touched the frame, so a
    # single-view part was blown up to 2.25:1 and its readings then had nowhere to go:
    # 柱塞2 and 手摇曲柄 both printed with the numbers piled on the outline and running off
    # the sheet.  A drawing needs a corridor around every view for a dimension line plus
    # its text -- roughly 20 mm on each side -- so that band is taken out of the usable
    # area before the scale is chosen.
    corridor = 20.0
    x_lo += corridor
    x_hi -= corridor
    y_lo += corridor
    y_hi -= corridor
    if x_hi <= x_lo:
        x_lo, x_hi = binding + inset, sheet_w - margin - inset
    if y_hi <= y_lo:
        y_lo, y_hi = margin + inset, sheet_h - margin - inset
    # The GB title block occupies the bottom-right corner (180 x 56).  Reserving only a
    # horizontal band across the whole sheet wasted the left half; the block has to be
    # kept out of that corner specifically, which is why a view drawn at the sheet centre
    # used to overlap it.
    title_box = None
    if reserve_title:
        title_box = (sheet_w - margin - 180.0, margin, sheet_w - margin, margin + 56.0)
    a_w, a_h = by_name[anchor]

    up = sorted([(n, by_name[n]) for n in names
                 if n != anchor and projection_alignment(n) == ("x", 1)])
    down = sorted([(n, by_name[n]) for n in names
                   if n != anchor and projection_alignment(n) == ("x", -1)])
    left = sorted([(n, by_name[n]) for n in names
                   if n != anchor and projection_alignment(n) in (("y", -1), ("y", 2))])
    right = sorted([(n, by_name[n]) for n in names
                    if n != anchor and projection_alignment(n) == ("y", 1)])
    # ---- TWO views go SIDE BY SIDE.
    #
    # First-angle projection puts the plan below the elevation, which is correct for a
    # three-view sheet but wasteful when there are only two: the pair ends up as a tall
    # narrow block down the middle of the sheet, using a third of the width, and each view
    # has corridors only left and right of the stack.  Laid out horizontally the same two
    # views get the full width, the corridors between them have room, and the sheet reads
    # left to right the way a two-view drawing normally does.  A caller who wants strict
    # projection can pass projection:"first" with layout:"projection".
    horizontal_pair = (len(names) == 2
                       and str(pair_layout or "horizontal").lower() != "projection")
    if horizontal_pair and (up or down):
        # Move the vertical partner into the "right" slot so the shared-axis logic below
        # places it beside the anchor instead of under it.
        right = right + up + down
        up = []
        down = []
    # The gap has to hold a dimension run plus its text, not a visual breather.  Too wide
    # and the section view no longer fits beside the others, so it is capped: 16 mm is
    # enough for two stacked readings and keeps the aligned block inside the frame.
    #
    # A side-by-side pair is the exception: it has the whole sheet width to spend, and the
    # corridor between the two views is where their inner dimensions live.  It therefore
    # gets 150 % of the requested gap, and the widening is applied BEFORE the cap --
    # capping first turned 42 mm into 24 and then 36, which is why the first attempt at
    # 150 % produced no visible change at all.
    requested_gap = gap
    if horizontal_pair:
        gap = max(gap, requested_gap * 1.5)
    gap = max(16.0, min(gap, 24.0 if not horizontal_pair else 72.0))
    positions = {}

    def stack_extent(items, index):
        return sum(item[1][index] for item in items) + gap * max(0, len(items) - 1)

    # ---- the chain is measured first and centred as one block.  Stacking upward from a
    # sheet-centred anchor looks equivalent but is not: a tall stack then runs off the top
    # edge and the caller falls back to the grid, which is the misalignment being fixed.
    up_h, down_h = stack_extent(up, 1), stack_extent(down, 1)
    left_w, right_w = stack_extent(left, 0), stack_extent(right, 0)
    total_h = a_h + (gap + up_h if up else 0.0) + (gap + down_h if down else 0.0)
    total_w = a_w + (gap + left_w if left else 0.0) + (gap + right_w if right else 0.0)
    cy = (y_lo + y_hi) / 2.0
    cx = (x_lo + x_hi) / 2.0
    # ---- a side-by-side pair sits at 55 % of the sheet height.
    #
    # Centring the pair left the block low on the paper with the title block crowding its
    # bottom corridor, while the top third of the sheet stayed empty.  Raising the pair to
    # 55 % puts it slightly above centre, which is where a two-view drawing reads best:
    # the extra room below is exactly the corridor the bottom dimensions need.
    #
    # The target is applied to the VIEW CENTRES, not to the block's midpoint.  A pair of
    # unequal views (泵体's plan is 111 mm tall, its elevation 44 mm) has its block centre
    # well below the two centres, so moving the block to 55 % put the actual views at
    # roughly 60 % and squeezed the corridor above them -- which is where the two
    # overlapping readings came from.
    if horizontal_pair:
        wanted_cy = sheet_h * 0.55
        if up or down:
            tallest_h = max([a_h] + [h for _n, (_w, h) in (up + down)])
        else:
            tallest_h = a_h
        lowest_allowed = y_lo + tallest_h / 2.0
        highest_allowed = y_hi - tallest_h / 2.0
        cy = max(lowest_allowed, min(wanted_cy, highest_allowed))

    # vertical chain: bottom of the block upward through Down, anchor, Up
    cursor = cy - total_h / 2.0
    for name, (w, h) in down[::-1]:
        positions[name] = (cx, cursor + h / 2.0)
        cursor += h + gap
    anchor_cy = cursor + a_h / 2.0
    cursor += a_h + gap
    for name, (w, h) in up:
        positions[name] = (cx, cursor + h / 2.0)
        cursor += h + gap
    # horizontal chain: left of the block rightward through Left, anchor, Right
    cursor = cx - total_w / 2.0
    for name, (w, h) in left[::-1]:
        positions[name] = (cursor + w / 2.0, anchor_cy)
        cursor += w + gap
    anchor_cx = cursor + a_w / 2.0
    cursor += a_w + gap
    for name, (w, h) in right:
        positions[name] = (cursor + w / 2.0, anchor_cy)
        cursor += w + gap
    positions[anchor] = (anchor_cx, anchor_cy)

    # ---- lift the whole block clear of the title block.  Centring alone put the bottom
    # of a three-view drawing straight onto the 180 x 56 GB title block.
    #
    # The lift has to move the WHOLE chain, and the projection partners are re-locked
    # afterwards: shifting only the views that clash breaks the shared centre line, which
    # is how Right ended up 19 mm off Front's height.
    if title_box is not None:
        # A view whose bottom sits above the title block can still have its dimension
        # corridor land ON the block: readings are placed below the view, so clearing the
        # view border by a millimetre is not enough -- 泵体's "35" and 固定板's "39" printed
        # across the title block with the views themselves clear of it.  The lift therefore
        # leaves a full corridor plus a text height of slack, and only views that actually
        # overlap the block's X range need it.
        slack = corridor + (3.5 * 2.0)
        blocking = []
        for name in positions:
            half_w = by_name[name][0] / 2.0
            left_edge = positions[name][0] - half_w
            right_edge = positions[name][0] + half_w
            overlaps_x = not (right_edge < title_box[0] or left_edge > title_box[2])
            if not overlaps_x:
                continue
            bottom_edge = positions[name][1] - by_name[name][1] / 2.0
            blocking.append((name, bottom_edge))
        if blocking:
            lowest = min(edge for _name, edge in blocking)
            if lowest < title_box[3] + slack:
                shift = title_box[3] + slack - lowest
                for name in positions:
                    positions[name] = (positions[name][0], positions[name][1] + shift)
                anchor_cy += shift

    # ---- lock the partners onto the anchor's axis.  This runs AFTER the lift so the
    # shared centre line survives it: Top/Bottom share the anchor's X, Left/Right its Y.
    for name in list(positions):
        if name == anchor:
            continue
        kind = projection_alignment(name)
        x, y = positions[name]
        if horizontal_pair:
            # Side by side: the partner shares the anchor's HEIGHT and keeps the X the
            # chain gave it.  Re-locking on projection_alignment would put it straight
            # back under the anchor -- that is exactly what happened on the first try,
            # where both views ended up at the same centre and printed on top of each
            # other.
            positions[name] = (x, anchor_cy)
            continue
        if kind and kind[0] == "x":
            positions[name] = (anchor_cx, y)
        elif kind and kind[0] == "y":
            positions[name] = (x, anchor_cy)
    # ---- free views (isometric, sections): the emptiest remaining corner.
    #
    # Four fixed corners were not enough: on the pump body the section is wide and short
    # and the plan view fills the middle, so every corner clashed and the fallback parked
    # the section on top of another view.  The free views scan the usable area instead.
    free = [(n, by_name[n]) for n in names if n not in positions]
    for name, (w, h) in free:
        spot = None
        step_scan = max(6.0, gap * 0.6)
        # A free view on a two-view sheet belongs BESIDE the anchor, at the same height and
        # the widened gap apart -- the same rule the projection partners get.  The plain
        # corner scan below found the first clear spot, which for 泵体 was 36 mm from the
        # plan view and slightly off its centre line, so the two views' dimension corridors
        # ran into each other even though neither view overlapped.
        if len(names) == 2 and horizontal_pair and positions:
            anchor_name = anchor
            anchor_spot = positions.get(anchor_name)
            if anchor_spot is not None:
                anchor_w = by_name[anchor_name][0]
                candidates = [
                    (anchor_spot[0] + anchor_w / 2.0 + gap + w / 2.0, anchor_spot[1]),
                    (anchor_spot[0] - anchor_w / 2.0 - gap - w / 2.0, anchor_spot[1]),
                ]
                for cand_x, cand_y in candidates:
                    if not (x_lo + w / 2.0 - 1e-6 <= cand_x <= x_hi - w / 2.0 + 1e-6):
                        continue
                    if not (y_lo + h / 2.0 - 1e-6 <= cand_y <= y_hi - h / 2.0 + 1e-6):
                        continue
                    box = (cand_x - w / 2.0, cand_y - h / 2.0,
                           cand_x + w / 2.0, cand_y + h / 2.0)
                    clash = False
                    for other, (pw, phh) in by_name.items():
                        other_spot = positions.get(other)
                        if other_spot is None:
                            continue
                        if boxes_overlap(box, (other_spot[0] - pw / 2.0,
                                               other_spot[1] - phh / 2.0,
                                               other_spot[0] + pw / 2.0,
                                               other_spot[1] + phh / 2.0)):
                            clash = True
                            break
                    if not clash and title_box is not None and boxes_overlap(box, title_box):
                        clash = True
                    if not clash:
                        spot = (cand_x, cand_y)
                        break
        y_cursor = y_hi - h / 2.0
        while spot is None and y_cursor >= y_lo + h / 2.0 - 1e-6:
            x_cursor = x_lo + w / 2.0
            while x_cursor <= x_hi - w / 2.0 + 1e-6:
                box = (x_cursor - w / 2.0, y_cursor - h / 2.0,
                       x_cursor + w / 2.0, y_cursor + h / 2.0)
                clash = False
                for other, (pw, phh) in by_name.items():
                    other_spot = positions.get(other)
                    if other_spot is None:
                        continue
                    if boxes_overlap(box, (other_spot[0] - pw / 2.0, other_spot[1] - phh / 2.0,
                                           other_spot[0] + pw / 2.0,
                                           other_spot[1] + phh / 2.0)):
                        clash = True
                        break
                if not clash and title_box is not None and boxes_overlap(box, title_box):
                    clash = True
                if not clash:
                    spot = (x_cursor, y_cursor)
                    break
                x_cursor += step_scan
            y_cursor -= step_scan
        if spot is None:
            return positions, False, 0.0, (0.0, 0.0)
        positions[name] = spot
    # ---- feasible when every box stays inside the frame and clear of the title block.
    # A view on the projection chain is not tested against its partners: Top sits below the
    # elevation and Right beside it, so their borders meet by construction, and rejecting
    # that pair drove the scale down to 0.7 on a sheet with room for 1.25.
    feasible = True
    chained = set([anchor]) | {n for n in names if projection_alignment(n)}
    for name, (w, h) in by_name.items():
        x, y = positions[name]
        if (x - w / 2.0 < binding - 1e-6 or x + w / 2.0 > sheet_w - margin + 1e-6
                or y - h / 2.0 < margin - 1e-6 or y + h / 2.0 > sheet_h - margin + 1e-6):
            feasible = False
            break
        if title_box is not None:
            box = (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)
            if boxes_overlap(box, title_box):
                feasible = False
                break
    view_area = sum(width * height for _, width, height in entries)
    xs = [positions[n][0] for n in positions]
    ys = [positions[n][1] for n in positions]
    block = (max(xs) - min(xs), max(ys) - min(ys)) if xs else (0.0, 0.0)
    return positions, feasible, view_area, block


def plan_grid(entries, sheet_w, sheet_h, gap, reserve_title, columns):
    """Rows/columns of boxes inside the frame: (positions, feasible, view_area, block).

    entries is [(name, width, height)] in placement order. Column widths and row heights
    come from the largest box in that band, and the leftover space is divided equally into
    the gaps so the block sits evenly on the sheet instead of hugging one corner.
    """
    if not entries:
        return {}, False, 0.0, (0.0, 0.0)
    rows = int(math.ceil(len(entries) / float(columns)))
    col_w = [0.0] * columns
    row_h = [0.0] * rows
    for index, (_, width, height) in enumerate(entries):
        col_w[index % columns] = max(col_w[index % columns], width)
        row_h[index // columns] = max(row_h[index // columns], height)
    block_w = sum(col_w) + gap * (columns - 1)
    block_h = sum(row_h) + gap * (rows - 1)
    binding, margin = sheet_margins(sheet_w, sheet_h)
    # The views keep clear of the frame by at least 10 % of the usable sheet width.
    usable_w = sheet_w - binding - margin
    inset = max(2.0, 0.10 * usable_w)
    x0, x1 = binding + inset, sheet_w - margin - inset
    y0, y1 = margin + inset, sheet_h - margin - inset
    if reserve_title:
        # The GB title block is 180 x 56 in the bottom-right corner, so the views stay
        # above it and the BOM table can hug its top edge.
        y0 = max(y0, margin + 56.0 + gap * 0.5)
    # Room for the dimension corridors, same as aligned_positions: without it a single
    # view grows until its border meets the frame and the readings have nowhere to go.
    corridor = 20.0
    x0 += corridor
    x1 -= corridor
    y0 += corridor
    y1 -= corridor
    if x1 <= x0 or y1 <= y0:
        x0, x1 = binding + inset, sheet_w - margin - inset
        y0 = max(margin + inset, margin + 56.0 + gap * 0.5 if reserve_title else margin + inset)
        y1 = sheet_h - margin - inset
    avail_w, avail_h = x1 - x0, y1 - y0
    feasible = block_w <= avail_w + 1e-6 and block_h <= avail_h + 1e-6
    extra_x = max(0.0, avail_w - block_w) / (columns + 1)
    extra_y = max(0.0, avail_h - block_h) / (rows + 1)
    positions = {}
    top = y1 - extra_y
    for row in range(rows):
        row_bottom = top - row_h[row]
        cursor_x = x0 + extra_x
        for col in range(columns):
            index = row * columns + col
            if index >= len(entries):
                break
            name = entries[index][0]
            positions[name] = (cursor_x + col_w[col] / 2.0, row_bottom + row_h[row] / 2.0)
            cursor_x += col_w[col] + gap + extra_x
        top = row_bottom - gap - extra_y
    view_area = sum(width * height for _, width, height in entries)
    return positions, feasible, view_area, (block_w, block_h)


def layout_views_with_rules(sheet, views, reserve=True, min_area=None, gap_ratio=None,
                            min_columns=2, max_columns=4, pair_layout="horizontal"):
    """Scale the views up until they cover `min_area` of the sheet, then place them.

    The scale ladder is walked from large to small; for each ratio the grid is tried with
    2, 3 and then 4 columns and the first rung that both fits and reaches the area target
    is applied. If no rung reaches it, the roomiest that fits is used and the shortfall is
    reported in `areaRatio` / `met`.
    """
    width, height = float(sheet.Length), float(sheet.Height)
    gap = (LAYOUT_GAP_RATIO if gap_ratio is None else float(gap_ratio)) * width
    want = LAYOUT_MIN_AREA if min_area is None else float(min_area)
    target_area = want * width * height
    ordered = order_views(views)
    boxes = view_boxes(ordered)
    live = [(view, boxes[view.Name]) for view in ordered if boxes.get(view.Name)]
    if not live:
        return {"placed": {}, "areaRatio": 0.0, "target": want, "met": False,
                "gap": round(gap, 2), "note": "no view borders were readable"}

    base = {view.Name: (true_scale(view) or 1.0) for view, _ in live}
    sizes = {view.Name: (box[2] - box[0], box[3] - box[1]) for view, box in live}

    # ---- a ceiling on the scale, so the drawing stays readable.
    #
    # The ladder below always takes the largest rung that fits, which on a small part means
    # blowing it up until its border touches the frame.  The result was drawings at 6:1 and
    # 3.75:1 whose readings had nowhere to sit (弹簧塞, 支撑板, 手摇曲柄 in the panel's
    # folder): the numbers piled on the outline and some ran off the sheet.  A dimensioned
    # drawing is not supposed to fill the paper -- the corridors have to fit too -- so the
    # effective factor is capped by the room the corridors leave.
    corridor = 20.0
    usable_w = width - sheet_margins(width, height)[0] - sheet_margins(width, height)[1]
    usable_h = height - sheet_margins(width, height)[0] - sheet_margins(width, height)[1]
    widest = max((sizes[name][0] for name in sizes), default=0.0)
    tallest = max((sizes[name][1] for name in sizes), default=0.0)
    # Each view needs its own corridor on both sides; with two views stacked the tall one
    # decides the vertical budget.  The share of the sheet a single view may take is
    # deliberately modest: a dimensioned drawing is read, not filled edge to edge.
    budget_w = max(40.0, usable_w - 2.0 * corridor) * 0.72
    budget_h = max(40.0, usable_h - 2.0 * corridor) * 0.62
    cap = 1.0
    if widest > 0 and tallest > 0:
        # How much of the sheet the views share depends on the arrangement: side by side
        # they compete for width, stacked they compete for height.
        if len(live) == 2 and str(pair_layout or "horizontal").lower() != "projection":
            # The pair now sits 150 % of the gap apart, and that gap comes out of the same
            # width budget as the views themselves.
            pair_gap = gap * 1.5
            cap = min(max(20.0, budget_w - pair_gap) / (widest * 2.0),
                      budget_h / tallest)
        else:
            columns_needed = 1.0 if len(live) <= 1 else 2.0
            cap = min(budget_w / (widest * columns_needed), budget_h / tallest)
    cap = max(0.1, min(cap, 2.0))
    out("layout: scale cap x%.3f (widest %.1f, tallest %.1f, %d view(s))"
        % (cap, widest, tallest, len(live)))

    best = None
    aligned_best = None
    for factor in SCALE_LADDER:
        if factor > cap + 1e-9:
            continue
        entries = [(view.Name, sizes[view.Name][0] * factor, sizes[view.Name][1] * factor)
                   for view, _ in live]
        # Projection alignment is the layout a drawing must have: Top straight below
        # Front, Right straight beside it, each sharing the anchor's centre line.  It is
        # kept in its own slot so a larger *grid* fit can never win on area alone -- that
        # is what silently put the views side by side on a wide sheet.
        positions, feasible, area, block = aligned_positions(
            entries, width, height, gap, reserve, [v.Name for v, _ in live],
            pair_layout=pair_layout)
        if feasible and (aligned_best is None or area > aligned_best[0]):
            aligned_best = (area, factor, 0, positions)

    if aligned_best is not None:
        best = aligned_best
    else:
        for factor in SCALE_LADDER:
            if factor > cap + 1e-9:
                continue
            entries = [(view.Name, sizes[view.Name][0] * factor, sizes[view.Name][1] * factor)
                       for view, _ in live]
            for columns in range(min_columns, max_columns + 1):
                if columns > len(entries):
                    continue
                grid_positions, grid_ok, grid_area, grid_block = plan_grid(
                    entries, width, height, gap, reserve, columns)
                if not grid_ok:
                    continue
                if best is None or grid_area > best[0]:
                    best = (grid_area, factor, columns, grid_positions)

    pick = best
    if pick is None:
        return {"placed": {}, "areaRatio": 0.0, "target": want, "met": False,
                "gap": round(gap, 2),
                "note": "no scale on the ladder fits the views at the required gap"}

    _, factor, columns, positions = pick
    placed = {}
    for view, _ in live:
        ratio = base[view.Name] * factor
        if abs(ratio - base[view.Name]) > 1e-6:
            try:
                uf_session().Draw.SetViewScale(view.Tag, float(ratio))
            except Exception as error:
                out("scale set failed:", view.Name, repr(error))
        x, y = positions[view.Name]
        try:
            view.MoveView(NX.Point3d(float(x), float(y), 0.0))
            placed[view.Name] = [round(float(x), 2), round(float(y), 2)]
        except Exception as error:
            out("view move failed:", view.Name, repr(error))

    try:
        after = view_boxes([view for view, _ in live])
        real = sum((box[2] - box[0]) * (box[3] - box[1])
                   for box in after.values() if box)
    except Exception as error:
        out("layout measure after moving failed:", repr(error))
        real = 0.0
    ratio_of_sheet = real / (width * height) if width and height else 0.0
    out("layout: %d view(s), %d column(s), gap %.1f mm (%.0f%% of width), scale x%.3f, "
        "views cover %.1f%% of %.0fx%.0f"
        % (len(placed), columns, gap, 100.0 * gap / width, factor,
           100.0 * ratio_of_sheet, width, height))
    return {"placed": placed, "gap": round(gap, 2), "columns": columns,
            "scaleFactor": round(factor, 4), "target": round(want, 4),
            "areaRatio": round(ratio_of_sheet, 4), "met": ratio_of_sheet >= want - 1e-3}


def auto_layout_views(sheet, views, reserve=True, gap=None, pad=8.0):
    """Backwards-compatible wrapper: apply the drawing rules and return where views landed."""
    report = layout_views_with_rules(sheet, views, reserve=reserve)
    return report.get("placed", {})


def cmd_view_scale(args):
    part = display_part()
    view = pick_view(part, args.get("view"))
    raw_scale = args.get("scale")
    if raw_scale is None:
        # The tool schema splits this into a number (`scale`) and a ratio string (`ratio`);
        # accept either, otherwise "1:2" through `ratio` was rejected as missing.
        raw_scale = args.get("ratio")
    if raw_scale is None:
        raise RuntimeError("scale is required — a ratio like 2, 1.5 or \"1:2\"")
    ratio = ratio_value(raw_scale)
    if ratio <= 0:
        raise RuntimeError("scale must be positive, got %r" % (raw_scale,))
    before = true_scale(view)
    uf_session().Draw.SetViewScale(view.Tag, float(ratio))
    after = true_scale(view)
    # Drawn size on the sheet, so the caller can see the effect without opening NX.
    box = None
    try:
        b = uf_session().Draw.AskViewBorders(view.Tag)
        box = [round(float(b[0]), 2), round(float(b[1]), 2),
               round(float(b[2]), 2), round(float(b[3]), 2)]
    except Exception:
        pass
    return {"view": fix_text(view.Name), "requested": round(ratio, 4),
            "before": round(before, 4), "scale": round(after, 4),
            "border": box, "visible": len(list(view.AskVisibleObjects()))}


def cmd_view_move(args):
    part = display_part()
    view = pick_view(part, args.get("view"))
    at = args.get("at") or args.get("to")
    if not at or len(at) < 2:
        raise RuntimeError("at [x,y] is the new view centre on the sheet, e.g. [320,140]")
    before = view.GetDrawingReferencePoint()
    from_before = [round(float(before.X), 2), round(float(before.Y), 2)]
    view.MoveView(NX.Point3d(float(at[0]), float(at[1]), 0.0))
    after = view.GetDrawingReferencePoint()
    return {"view": fix_text(view.Name), "requested": [round(float(at[0]), 2), round(float(at[1]), 2)],
            "before": from_before,
            "sheetPoint": [round(float(after.X), 2), round(float(after.Y), 2)],
            "scale": round(true_scale(view), 4)}


def cmd_sheet_delete(args):
    part = display_part()
    sheets = list(part.DraftingDrawingSheets)
    if not sheets:
        raise RuntimeError("the part has no drawing sheets")
    names = ", ".join(fix_text(x.Name) for x in sheets)
    wanted = args.get("name") or args.get("sheet")
    if wanted:
        key = str(wanted).strip().lower()
        target = None
        for candidate in sheets:
            if fix_text(candidate.Name).lower() == key:
                target = candidate
                break
        if target is None:
            raise RuntimeError("no sheet named %r (sheets: %s)" % (wanted, names))
    else:
        if len(sheets) <= 1:
            raise RuntimeError("refusing to delete the only sheet (%s)" % names)
        target = sheets[-1]
    if len(sheets) <= 1:
        raise RuntimeError("refusing to delete the only sheet (%s)" % names)
    gone = fix_text(target.Name)
    uf_session().Draw.DeleteDrawing(target.Tag)
    left = [fix_text(x.Name) for x in list(part.DraftingDrawingSheets)]
    return {"deleted": gone, "sheets": left}


def cmd_section_vertical(args):
    """A vertical cut, which the SectionViewBuilder path refuses ("Invalid hinge line").

    UF_DRAW_create_simple_sxview takes the cut position as a *drawing* point and works out
    the model plane from it, so the point has to be placed inside the parent view's frame —
    when it is not, NX still commits the view and hands back an empty frame with no error.
    """
    part = display_part()
    parent = pick_view(part, args.get("parent"))
    sheet = current_sheet(part)
    if sheet is None:
        raise RuntimeError("the part has no drawing sheet — run nx_drawing first")
    cut = args.get("cutX", args.get("cut_x"))
    cut_y = args.get("cutY", args.get("cut_y"))
    if cut is None and cut_y is None:
        raise RuntimeError("cutX (or cutY) is the model coordinate the cut plane sits at, "
                           "e.g. -70")
    axis = "y" if (cut is None or str(args.get("axis") or "").lower() == "y") else "x"
    if axis == "y":
        cut = float(cut_y if cut_y is not None else cut)
    else:
        cut = float(cut)
    scale = args.get("scale")
    scale = true_scale(parent) if scale is None else ratio_value(scale)
    at = args.get("at") or list(free_spot(part, view_need(parent, part)))
    at = (float(at[0]), float(at[1]))

    # Centre of the parent view in MODEL coordinates, so the cut point can be placed on the
    # sheet.  A cut at constant model X moves the point along the sheet's X; one at constant
    # model Y moves it along the sheet's Y (the parent is a plan view, so the sheet axes are
    # the model X and Y axes).
    centre_x = centre_y = 0.0
    try:
        box = measure(part)
        lo = box.get("bboxMin") or [0.0, 0.0, 0.0]
        size = box.get("bboxSize") or [0.0, 0.0, 0.0]
        centre_x = float(lo[0]) + float(size[0]) / 2.0
        centre_y = float(lo[1]) + float(size[1]) / 2.0
    except Exception:
        pass
    ref = parent.GetDrawingReferencePoint()
    rx, ry = float(ref.X), float(ref.Y)
    if axis == "y":
        px = rx
        py = ry + (cut - centre_y) * scale
    else:
        px = rx + (cut - centre_x) * scale
        py = ry
    inside = True
    box_on_sheet = None
    try:
        b = uf_session().Draw.AskViewBorders(parent.Tag)
        box_on_sheet = [round(float(b[i]), 2) for i in range(4)]
        inside = (float(b[0]) <= px <= float(b[2])) and (float(b[1]) <= py <= float(b[3]))
    except Exception:
        pass
    if not inside:
        raise RuntimeError(
            "the cut point (%.1f, %.1f) lands outside %s (frame %s); NX would return an "
            "empty section view without an error — pick a cutX closer to the part or a "
            "parent view that covers it"
            % (px, py, fix_text(parent.Name),
               "unknown" if box_on_sheet is None else box_on_sheet))

    import NXOpen.UF as UFLIB
    cut_obj = UFLIB.DrfObject_Struct()
    cut_obj.ObjectAssocType = UFLIB.DrfAssocType.DWG_POS
    cut_obj.AssocDwgPos = [px, py]

    # The step and arrow directions are in the parent view's sheet frame.  A cut at
    # constant model Y runs ACROSS the sheet's X, so its step direction and viewing
    # direction are the X-axis ones; a cut at constant model X is the other way round.
    #
    # The arrow points the way the section is LOOKED AT, and getting it backwards prints
    # the cut upside down: with [0,-1] the pump body's M16 thread (which is at the top of
    # the part) came out at the bottom of the sheet.  Looking along +Y keeps the parent's
    # up direction up.
    if axis == "y":
        step_dir = args.get("stepDir") or [1.0, 0.0, 0.0]
        arrow_dir = args.get("arrowDir") or [0.0, 1.0, 0.0]
    else:
        step_dir = args.get("stepDir") or [0.0, 1.0, 0.0]
        arrow_dir = args.get("arrowDir") or [1.0, 0.0, 0.0]
    known = {v.Tag for v in drafting_views(part)}
    tag = uf_session().Draw.CreateSimpleSxview(
        sheet.Tag, float(scale),
        [float(v) for v in step_dir], [float(v) for v in arrow_dir],
        parent.Tag, cut_obj, [at[0], at[1]])
    fresh = [v for v in drafting_views(part) if v.Tag not in known]
    if not fresh:
        raise RuntimeError("CreateSimpleSxview returned tag %r but no new view appeared" % (tag,))
    view = fresh[-1]
    visible = len(list(view.AskVisibleObjects()))
    if visible < 5:
        raise RuntimeError(
            "the section view came out empty (%d visible object(s)) — the cut point "
            "(%.1f, %.1f) misses the model; move it with cutX" % (visible, px, py))
    # The section lands at `at`; grid the whole sheet again so it joins the layout instead
    # of sitting wherever the caller guessed. Pass layout:"none" to keep manual placement.
    if str(args.get("layout") or "").lower() not in ("none", "keep"):
        try:
            auto_layout_views(sheet, list(sheet.GetDraftingViews()))
        except Exception as e:
            out("auto layout after section skipped:", repr(e))
    return {"view": view_summary(view), "parent": fix_text(parent.Name),
            "cutAxis": axis, "cutX" if axis == "x" else "cutY": cut,
            "cutPoint": [round(px, 2), round(py, 2)],
            "parentFrame": box_on_sheet, "scale": round(scale, 4),
            "at": [round(at[0], 2), round(at[1], 2)], "visible": visible}


def cmd_note(args):
    part = display_part()
    text = args.get("text")
    lines = text.split("\n") if isinstance(text, str) else list(text or [])
    lines = [str(line) for line in lines if str(line).strip()]
    if not lines:
        raise RuntimeError("text is required (a string with \\n, or a list of lines)")
    size = float(args.get("size") or 3.5)
    at = args.get("at") or [20.0, 275.0]
    if len(at) < 3:
        at = [at[0], at[1], 0.0]
    font = args.get("font")
    if not font and any(ord(ch) > 127 for ch in "".join(lines)):
        # NX's latin stroke fonts have no CJK glyphs and draw boxes instead.
        font = "chinesef"

    builder = part.Annotations.CreateDraftingNoteBuilder(None)
    used = None
    if font:
        try:
            index = part.Fonts.AddFont(str(font), NX.FontCollectionType.Nx)
        except Exception as error:
            raise RuntimeError("font %r is not an NX character font (try chinesef, chineset, "
                               "kanji, blockfont, Arial): %s" % (font, error))
        builder.Style.LetteringStyle.GeneralTextFont = int(index)
        used = {"name": str(font), "index": int(index)}
    try:
        builder.Style.LetteringStyle.GeneralTextSize = size
    except Exception as error:
        out("text size not set:", repr(error))
    builder.Text.SetEditorText(lines)
    builder.Origin.OriginPoint = pnt(float(at[0]), float(at[1]), float(at[2]) if len(at) > 2 else 0.0)
    builder.Validate()
    note = builder.Commit()
    return {"lines": len(lines), "text": lines, "font": used, "size": size,
            "at": [round(float(v), 2) for v in at], "note": str(note)}


# ------------------------------------------------------------------
# modelling
# ------------------------------------------------------------------
def pnt(x, y, z):
    point = NX.Point3d()
    point.X, point.Y, point.Z = float(x), float(y), float(z)
    return point


def vertex_point(vertex):
    """Edge.GetVertices() hands back bare Point3d values in this build, but older
    ones wrap a Vertex whose Coordinates hold the point."""
    if hasattr(vertex, "X") and hasattr(vertex, "Y") and hasattr(vertex, "Z"):
        return (vertex.X, vertex.Y, vertex.Z)
    point = vertex.Coordinates
    return (point.X, point.Y, point.Z)


def annotation_view(part, wanted):
    """The drafting view a label should ride on (None keeps it free on the sheet)."""
    if not wanted:
        return None
    return pick_view(part, wanted)


def place_origin(builder, view, at):
    builder.Origin.OriginPoint = pnt(float(at[0]), float(at[1]), float(at[2]) if len(at) > 2 else 0.0)
    if view is not None:
        try:
            builder.Origin.AnnotationView = view
        except Exception as error:
            out("annotation view not attached:", repr(error))


def cmd_gdt(args):
    """A GD&T feature control frame: characteristic, tolerance and frame count."""
    part = display_part()
    raw = str(args.get("characteristic") or "position").strip().lower().replace("-", "_").replace(" ", "_")
    characteristic = FCF_CHARACTERISTICS.get(raw)
    if not characteristic:
        raise RuntimeError("unknown characteristic %r — use one of: %s"
                           % (raw, ", ".join(sorted(FCF_CHARACTERISTICS))))
    at = args.get("at") or free_spot(part, (40.0, 25.0))
    view = annotation_view(part, args.get("view"))
    builder = part.Annotations.CreateDraftingFeatureControlFrameBuilder(
        NXOpen.Annotations.Gdt.Null)
    builder.Characteristic = getattr(builder.FcfCharacteristic, characteristic)
    frames = builder.FeatureControlFrameDataList.GetContents()
    frame = frames[0]
    tolerance = args.get("tolerance")
    if tolerance is not None:
        frame.ToleranceValue = str(tolerance)
    place_origin(builder, view, at)
    gdt = builder.Commit()
    return {"characteristic": characteristic, "tolerance": tolerance,
            "frames": len(frames), "at": [round(float(v), 2) for v in at[:2]],
            "view": fix_text(view.Name) if view is not None else None,
            "annotation": str(gdt)}


def cmd_balloon(args):
    """A numbered balloon (item tag) on the sheet — the leader is optional."""
    part = display_part()
    lines = args.get("text")
    if lines is None:
        lines = args.get("number")
    if lines is None:
        lines = "1"
    if isinstance(lines, (list, tuple)):
        text_lines = [str(item) for item in lines if str(item).strip()]
    else:
        text_lines = [str(lines)]
    if not text_lines:
        raise RuntimeError("balloon text is required")
    at = args.get("at") or free_spot(part, (40.0, 25.0))
    size = float(args.get("size") or 5.0)
    view = annotation_view(part, args.get("view"))
    builder = part.Annotations.CreateDraftingNoteBuilder(None)
    builder.Text.SetEditorText(text_lines)
    try:
        builder.Style.LetteringStyle.GeneralTextSize = size
    except Exception as error:
        out("balloon text size not set:", repr(error))
    place_origin(builder, view, at)
    note = builder.Commit()
    return {"text": text_lines, "size": size,
            "at": [round(float(v), 2) for v in at[:2]],
            "view": fix_text(view.Name) if view is not None else None,
            "annotation": str(note)}


def cmd_surface_finish(args):
    """A surface finish (roughness) symbol: Ra value plus an optional process note."""
    part = display_part()
    value = str(args.get("value") or "3.2")
    at = args.get("at") or free_spot(part, (40.0, 25.0))
    view = annotation_view(part, args.get("view"))
    builder = part.Annotations.DraftingSurfaceFinishSymbol.CreateDraftingSurfaceFinishSymbolBuilder(None)
    builder.A1 = value
    method = args.get("method")
    if method:
        try:
            builder.B = str(method)
        except Exception as error:
            out("process note not set:", repr(error))
    place_origin(builder, view, at)
    symbol = builder.Commit()
    return {"value": value, "method": method,
            "at": [round(float(v), 2) for v in at[:2]],
            "view": fix_text(view.Name) if view is not None else None,
            "annotation": str(symbol)}


def cmd_centerline(args):
    """A centre mark on a drafting view, at a sheet point or a model circle centre."""
    part = display_part()
    view = pick_view(part, args.get("view"))
    at = args.get("at")
    if not at or len(at) < 2:
        raise RuntimeError("at [x, y] on the sheet (mm) is required")
    point = part.Points.CreatePoint(pnt(float(at[0]), float(at[1]), 0.0))
    builder = part.Annotations.Centerlines.CreateCenterMarkBuilder(None)
    builder.Locations.Clear()
    builder.Locations.AddWithViews([point], [view])
    builder.Commit()
    return {"kind": "center_mark", "view": fix_text(view.Name),
            "at": [round(float(at[0]), 2), round(float(at[1]), 2)]}

# ------------------------------------------------------------------
# real dimensions
#
# Every route through NXOpen.Annotations.DimensionCollection dies here: the
# LinearDimensionBuilder refuses an Associativity it was handed ("The first object
# associativity type is invalid"), and CreateHorizontalDimension(dimensionData,
# origin) takes the process down with an access violation.  UF_DRF_create_* still
# works, so the dimensions below are built with UF.Drf and a hand-filled
# DrfObject_Struct.  Two details cost the most time:
#   * dimension3dOrigin is a DRAWING coordinate, not a model point -- a model point
#     puts the dimension metres off the paper;
#   * ObjectAssocModifier must be 1 (first end point) or the measurement reads 0.


# ------------------------------------------------------------------
# real dimensions
#
# Every route through NXOpen.Annotations.DimensionCollection dies here: the
# LinearDimensionBuilder refuses an Associativity it was handed ("The first object
# associativity type is invalid"), and CreateHorizontalDimension(dimensionData,
# origin) takes the process down with an access violation.  UF_DRF_create_* still
# works, so the dimensions below are built with UF.Drf and a hand-filled
# DrfObject_Struct.  Two details cost the most time:
#   * dimension3dOrigin is a DRAWING coordinate, not a model point -- a model point
#     puts the dimension metres off the paper;
#   * ObjectAssocModifier must be 1 (first end point) or the measurement reads 0.
def drf():
    import NXOpen.UF as UFLIB
    return UFLIB


def edge_class(edge):
    """1 = straight, 2 = arc.  The enum member does not compare, str() does."""
    try:
        return int(str(edge.SolidEdgeType))
    except Exception:
        return 0


def view_axes(view):
    """Model directions that run along the view's sheet X and sheet Y."""
    m = view.Matrix
    rows = ((m.Xx, m.Xy, m.Xz), (m.Yx, m.Yy, m.Yz), (m.Zx, m.Zy, m.Zz))
    return ((rows[0][0], rows[1][0], rows[2][0]),
            (rows[0][1], rows[1][1], rows[2][1]))


def matrix_axes(m):
    """Same as view_axes but for a plain Matrix3x3 (a modelling view has no drawing view)."""
    rows = ((m.Xx, m.Xy, m.Xz), (m.Yx, m.Yy, m.Yz), (m.Zx, m.Zy, m.Zz))
    return ((rows[0][0], rows[1][0], rows[2][0]),
            (rows[0][1], rows[1][1], rows[2][1]))


def is_orthographic(view):
    m = view.Matrix
    return abs(abs(m.Zx) + abs(m.Zy) + abs(m.Zz) - 1.0) < 1e-6


def view_sheet_point(view, u, v):
    """(u, v) in view space -> sheet mm."""
    ref = view.GetDrawingReferencePoint()
    origin = view.Origin
    scale = float(view.Scale)
    return (float(ref.X) + (float(u) + float(origin.X)) * scale,
            float(ref.Y) + (float(v) + float(origin.Y)) * scale)


def prototype_of(component):
    """The component's prototype part, or None when NX only handed back a stub.

    A component comes back as a bare `NXObject` instead of a `Part` when NX could not
    open its file while loading the assembly: the stub has no `Bodies`, no `LoadFully`
    and no `FullPath`, and the component is missing from every view's
    `AskVisibleObjects()` too.  On the panel's 装配.prt that hit exactly one component,
    阀体1: its file had been written into the batch OUTPUT folder, so the assembly --
    which lives in its own source folder -- found no 阀体1.prt beside itself.  The earlier
    write-up called this a broken prototype needing a re-assembly; it is nothing of the
    sort, the file is intact and NX had simply never opened it.

    This returns None for a stub on purpose.  Reopening it with
    UF_PART_open_single_component_as DOES work, but it rewrites the component's recorded
    path -- and once one component pointed into the batch output folder, the next pass
    dragged all thirteen there, which silently destroyed the assembly.  Callers that only
    need a stub's SIZE should use stub_part_box(), which measures the part file on its own
    and leaves every component reference untouched.
    """
    try:
        prototype = component.Prototype
    except Exception:
        return None
    if prototype is None:
        return None
    if type(prototype).__name__ == "Part":
        return prototype
    return None


def assembly_bodies(part):
    """Every body an assembly actually shows, as placed occurrences.

    Walks the component tree to its leaves and returns the occurrence of each prototype
    body, so edge positions are in assembly space -- which is what a drafting view
    projects.  Suppressed components and empty leaves are skipped.
    """
    rows = []
    orphaned = []
    assembly = getattr(part, "ComponentAssembly", None)
    if assembly is None:
        return rows

    def walk(component, depth=0):
        if depth > 12:
            return
        try:
            if component.IsSuppressed:
                return
        except Exception:
            pass
        try:
            kids = list(component.GetChildren())
        except Exception:
            kids = []
        if kids:
            for kid in kids:
                walk(kid, depth + 1)
            return
        # A component opens LIGHTWEIGHT: its prototype part is loaded but carries no
        # bodies until it is asked for.  Reading Prototype.Bodies straight away returned
        # an empty list for all fourteen components of the panel's 装配.prt, which is why
        # the assembly drawing looked like it had no geometry at all.
        prototype = prototype_of(component)
        if prototype is None:
            # A stub: NX never opened this component's file, so it contributes no geometry.
            # Reported rather than silently skipped -- a missing component is exactly what
            # made this assembly's drawing look wrong for two rounds.
            out("assembly_bodies: %r has no loaded prototype (stub) and is skipped"
                % (getattr(component, "Name", "?"),))
            return
        bodies = []
        try:
            bodies = list(prototype.Bodies)
        except Exception:
            bodies = []
        if not bodies:
            try:
                prototype.LoadFully()
                bodies = list(prototype.Bodies)
            except Exception as error:
                out("assembly_bodies: LoadFully failed for %r: %r"
                    % (getattr(component, "Name", "?"), error))
        for body in bodies:
            try:
                placed = component.FindOccurrence(body)
            except Exception:
                placed = None
            if placed is None:
                # No occurrence means the body is NOT shown by this assembly -- it is a
                # spare solid left in the part file (泵体 carries two).  Appending the raw
                # prototype body would mix part-space geometry into assembly space and
                # corrupt the projected profile, so the body is dropped and reported.
                orphaned.append((getattr(component, "Name", "?"), body.Tag))
                continue
            rows.append(placed)

    try:
        root = assembly.RootComponent
        children = list(root.GetChildren()) if root is not None else []
    except Exception as error:
        out("assembly_bodies: root failed:", repr(error))
        children = []
    for child in children:
        walk(child)
    out("assembly_bodies: %d component(s) at root, %d body occurrence(s)"
        % (len(children), len(rows)))
    if orphaned:
        out("assembly_bodies: %d body/bodies not placed by any component "
            "(part-file spares, dropped): %r" % (len(orphaned), orphaned[:8]))
    return rows


def ortho_profile(part, view, bodies=None):
    """Straight edges and true circles of one view, in view (u, v) coordinates.

    A drafting view exposes no projected edges in this session, so the profile is
    rebuilt from the model: every edge is projected with the view matrix, and edges
    landing on the same u (or v) are the same feature -- which is also what collapses
    the near and far rim of a through hole into a single circle.

    A hole only reads as a circle when the view looks down its own axis.  Judging that
    from "the two vertices of the arc coincide" is wrong: a full circle's vertices
    always coincide, so a hole seen side-on would be called round too.  The test used
    here groups the circular edges by the cylindrical face that owns them and asks
    whether all their centres land on one spot -- which is exactly "axis parallel to
    the line of sight".
    """
    UFLIB = drf()
    ux, uy = view_axes(view)
    tol = 1e-6
    vertical, horizontal, circles = {}, {}, {}
    # Edge-on bore rims: two of them are the only handles a turned part offers,
    # so the distance between them becomes that part's axial length.
    rims = []
    # An ASSEMBLY owns no bodies of its own: everything it shows belongs to its
    # components.  Walking `part.Bodies` alone therefore found no geometry at all and
    # cmd_dimension refused with "the views carry no straight edges or circles" on the
    # panel folder's 装配.prt.  The component occurrences are added when the part has no
    # bodies of its own, so an assembly drawing can be dimensioned like a part drawing.
    candidates = list(bodies) if bodies else list(part.Bodies)
    if not candidates:
        try:
            assembly = getattr(part, "ComponentAssembly", None)
            if assembly is not None:
                candidates = assembly_bodies(part)
        except Exception as error:
            out("ortho_profile: assembly bodies failed:", repr(error))
    # ---- ASSEMBLY COORDINATES.
    #
    # `assembly_bodies` returns component OCCURRENCES, but the edges of an occurrence still
    # report their points in the PROTOTYPE PART's own space.  Projecting those directly
    # meant every component was drawn at the origin on top of the others: the assembly
    # sheet carried dimensions of 101.5 / 98 / 50.8 while the assembly actually measures
    # 156.5 / 100.8 / 159.5, and the numbers referred to no feature at all.  Each body is
    # mapped through its component's placement before it is projected.
    #
    # The placement is (origin, matrix) from Component.GetPosition(), and a part point p
    # maps to origin + matrix * p.  Verified against Measurement.GetBoundingBoxProperties
    # on an occurrence body, which already answers in assembly space.
    placements = {}
    if not list(part.Bodies):
        for body in candidates:
            component = getattr(body, "OwningComponent", None)
            if component is None:
                continue
            try:
                key = component.Tag
            except Exception:
                continue
            if key in placements:
                continue
            try:
                origin, matrix = component.GetPosition()
                placements[key] = (origin, matrix)
            except Exception:
                placements[key] = None

    def to_assembly(body, point):
        """Map a point of `body` into assembly space (identity for a plain part)."""
        if not placements:
            return point
        component = getattr(body, "OwningComponent", None)
        if component is None:
            return point
        try:
            placement = placements.get(component.Tag)
        except Exception:
            placement = None
        if placement is None:
            return point
        origin, matrix = placement
        px, py, pz = float(point[0]), float(point[1]), float(point[2])
        return (float(origin.X) + float(matrix.Xx) * px + float(matrix.Yx) * py
                + float(matrix.Zx) * pz,
                float(origin.Y) + float(matrix.Xy) * px + float(matrix.Yy) * py
                + float(matrix.Zy) * pz,
                float(origin.Z) + float(matrix.Xz) * px + float(matrix.Yz) * py
                + float(matrix.Zz) * pz)

    for body in candidates:
        try:
            edges = list(body.GetEdges())
        except Exception:
            continue
        # ---- collect every whole circle, one entry per circle
        #
        # Grouping the circles by owning cylindrical face looked tidy but merged unrelated
        # holes: a mirrored bolt hole shares its face identity with its twin, so nine
        # circles on the pump flange collapsed into three.  Each circle is kept on its own
        # and only the bits that are genuinely concentric (a bore and its counterbore) are
        # merged afterwards, by comparing projected centres.
        for edge in edges:
            if edge_class(edge) != 2:
                continue
            try:
                vertices = list(edge.GetVertices())
                if len(vertices) != 2:
                    continue
                a = vertex_point(vertices[0])
                b = vertex_point(vertices[1])
                if abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol or abs(a[2] - b[2]) > tol:
                    continue          # a partial arc: not a hole rim in any view
            except Exception:
                continue
            try:
                arc = UFLIB.UFSession.GetUFSession().Curve.AskArcData(edge.Tag)
            except Exception:
                continue
            # AskArcData answers in ASSEMBLY space on an occurrence body, like
            # GetVertices().  (Confirmed on 阀体1: its arc centres land on Z = -30.5, which
            # is the assembly X of that component, not a part-space coordinate.)  So the
            # centre is projected as it comes -- applying the placement again here was
            # what made the first attempt report Ø34.5 and a 4 mm overall length.
            centre = arc.ArcCenter
            cu = centre[0] * ux[0] + centre[1] * ux[1] + centre[2] * ux[2]
            cv = centre[0] * uy[0] + centre[1] * uy[1] + centre[2] * uy[2]
            radius = float(arc.Radius)
            # Does this view see the circle AS a circle?  Ask the arc itself: sample a
            # point on it and compare its distance from the projected centre with the
            # radius.  Reading the arc's plane normal was wrong -- AskArcData hands back
            # the matrix tag of a shared XY plane, so every circle looked like it faced
            # +Z and Right/SX views lost all their diameters.
            if not circle_faces_view(part, edge, arc, ux, uy, cu, cv, radius):
                rims.append((radius, [(cu, cv)], [edge]))
                continue
            key = (round(radius, 3), round(cu, 3), round(cv, 3))
            if key not in circles:
                circles[key] = edge
        # ---- straight edges
        for edge in edges:
            try:
                vertices = list(edge.GetVertices())
            except Exception:
                continue
            if len(vertices) != 2 or edge_class(edge) == 2:
                continue
            # GetVertices() already answers in ASSEMBLY space even on an occurrence body
            # (GetLocations() does not -- it stays in the prototype part's space).  So the
            # points are used as they come; mapping them through the component placement
            # again would double-apply the transform.
            a = vertex_point(vertices[0])
            b = vertex_point(vertices[1])
            au = (a[0] * ux[0] + a[1] * ux[1] + a[2] * ux[2],
                  a[0] * uy[0] + a[1] * uy[1] + a[2] * uy[2])
            bu = (b[0] * ux[0] + b[1] * ux[1] + b[2] * ux[2],
                  b[0] * uy[0] + b[1] * uy[1] + b[2] * uy[2])
            if abs(au[0] - bu[0]) < tol and abs(au[1] - bu[1]) < tol:
                continue
            if abs(au[0] - bu[0]) < tol:
                vertical.setdefault(round(au[0], 3), []).append(edge)
            elif abs(au[1] - bu[1]) < tol:
                horizontal.setdefault(round(au[1], 3), []).append(edge)
    return {"vertical": vertical, "horizontal": horizontal, "circles": circles,
            "rims": rims}


def circle_faces_view(part, edge, arc, ux, uy, cu, cv, radius, transform=None):
    """True when this view sees the arc as a circle rather than an edge-on line.

    Tested geometrically: rebuild a point on the arc from its own matrix and start angle,
    project it, and check the distance to the projected centre.  A circle facing the view
    puts that point exactly `radius` away; a circle seen edge-on puts it somewhere between
    0 and 2*radius.

    `cu`/`cv` are the projected centre ALREADY in assembly space, so the sample point has
    to be mapped the same way -- `transform` is the component's placement for an assembly
    body and None for a plain part.
    """
    try:
        matrix = part.FindObject(int(arc.MatrixTag))
        if matrix is None:
            return True
        # The arc's own in-plane axes, then a point on the circle at StartAngle.
        ax = (float(matrix.Xx), float(matrix.Yx), float(matrix.Zx))
        ay = (float(matrix.Xy), float(matrix.Yy), float(matrix.Zy))
        angle = float(arc.StartAngle)
        centre = arc.ArcCenter
        r = float(arc.Radius)
        point = tuple(centre[i] + r * (math.cos(angle) * ax[i] + math.sin(angle) * ay[i])
                      for i in range(3))
        if transform is not None:
            point = transform(point)
        px = point[0] * ux[0] + point[1] * ux[1] + point[2] * ux[2]
        py = point[0] * uy[0] + point[1] * uy[1] + point[2] * uy[2]
        distance = math.hypot(px - cu, py - cv)
        return abs(distance - radius) <= max(0.05, radius * 0.02)
    except Exception:
        return True


def longest(edges):
    """The edge of a group that spans the most -- the outer outline, not a hole rim."""
    best = None
    best_length = -1.0
    for edge in edges:
        try:
            length = float(edge.GetLength())
        except Exception:
            length = 0.0
        if length > best_length:
            best, best_length = edge, length
    return best


def drf_object(edge, view, assoc=None):
    """A UF_DRF object: the edge plus which end of it to dimension to."""
    UFLIB = drf()
    item = UFLIB.DrfObject_Struct()
    item.ObjectTag = edge.Tag
    item.ObjectViewTag = view.Tag
    item.ObjectAssocType = assoc or UFLIB.DrfAssocType.END_POINT
    item.ObjectAssocModifier = 1
    item.Object2Tag = 0
    item.AssocDwgPos = [0.0, 0.0]
    return item


def drf_text():
    UFLIB = drf()
    text = UFLIB.DrfText_Struct()
    text.UserDimText = ""
    return text


def dimension_style(dimension, size=None):
    """GB wants 3.5 mm lettering on a dimension; the default font already draws Ø.

    The lettering object has to go back in through SetGeneralText: writing Size on the
    object handed out by GetGeneralText does not survive the round trip on its own.
    """
    if not size:
        return
    try:
        lettering = dimension.GetLetteringPreferences()
        general = lettering.GetGeneralText()
        general.Size = float(size)
        lettering.SetGeneralText(general)
        dimension.SetLetteringPreferences(lettering)
    except Exception as error:
        out("dimension lettering not set:", repr(error))


def dimension_count_prefix(dimension, prefix):
    """'2×' in front of the value -- appended text renders next to the measurement."""
    try:
        appended = dimension.GetAppendedText()
        appended.SetBeforeText([prefix])
        dimension.SetAppendedText(appended)
    except Exception as error:
        out("dimension prefix not set:", repr(error))


def place_dimension(part, view, kind, first, second, x, y, prefix=None, size=None):
    """Create one dimension and hand back what it measured."""
    UFLIB = drf()
    tool = UFLIB.UFSession.GetUFSession().Drf
    creator = {"horizontal": tool.CreateHorizontalDim,
               "vertical": tool.CreateVerticalDim,
               "diameter": tool.CreateDiameterDim}[kind]

    def as_pair(item):
        """A call site may pass an edge, or (edge, association type) when it is an arc."""
        if isinstance(item, (tuple, list)):
            return item[0], item[1]
        return item, None

    edge, assoc = as_pair(first)
    one = drf_object(edge, view, assoc)
    if kind == "diameter":
        tag = creator(one, drf_text(), [x, y, 0.0])
    else:
        edge2, assoc2 = as_pair(second)
        two = drf_object(edge2, view, assoc2)
        tag = creator(one, two, drf_text(), [x, y, 0.0])
    dimension = None
    for candidate in part.Dimensions:
        if candidate.Tag == tag:
            dimension = candidate
            break
    if dimension is None:
        raise RuntimeError("the dimension was created but NX did not register it")
    dimension_style(dimension, size)
    if prefix:
        dimension_count_prefix(dimension, prefix)
    if kind == "diameter":
        # Pin the leader's stub direction so the diameter line stays near its circle.
        #
        # NX draws a diameter dimension as a line ACROSS the circle plus a stub to the
        # text, and it picks the stub direction itself.  Left to choose, it often points the
        # stub at the far side, which is how 支撑板's Ø26 got a leader reaching across half
        # the sheet while its text sat near the rim.  Setting the orientation from the text
        # position keeps the stub on the side the text is actually on.
        try:
            from NXOpen import Annotations as _Ann
            view_ref = None
            try:
                view_ref = dimension.LeaderOrientation
            except Exception:
                view_ref = None
            options = {"FromTop": _Ann.LeaderOrientation.FromTop,
                       "FromBottom": _Ann.LeaderOrientation.FromBottom,
                       "FromLeft": _Ann.LeaderOrientation.FromLeft,
                       "FromRight": _Ann.LeaderOrientation.FromRight}
            # Which side of the circle the text sits on decides the stub.
            try:
                centre_pt = view.GetDrawingReferencePoint()
                cx, cy = float(centre_pt.X), float(centre_pt.Y)
            except Exception:
                cx, cy = float(x), float(y)
            dx = float(x) - cx
            dy = float(y) - cy
            if abs(dx) >= abs(dy):
                wanted = "FromRight" if dx >= 0 else "FromLeft"
            else:
                wanted = "FromTop" if dy >= 0 else "FromBottom"
            dimension.LeaderOrientation = options[wanted]
        except Exception as error:
            out("diameter leader orientation not set:", repr(error))
    lines = dimension.GetDimensionText()[0]
    return {"kind": kind, "value": lines[0] if lines else "",
            "at": [round(float(x), 2), round(float(y), 2)], "tag": int(tag)}


def dim_text_box(text, size, x, y, kind):
    """Sheet rectangle a dimension occupies, for overlap tests.

    A HORIZONTAL dimension prints its reading lying along the line: wide and flat.
    A VERTICAL dimension prints it rotated 90 degrees: narrow and tall.  Getting that
    backwards was why overlaps survived the tests -- the estimator reported the vertical
    readings as 14 mm wide and 6 mm tall when on the sheet they are the other way round,
    so four stacked vertical dimensions all looked clear while printing on top of
    each other.
    """
    size = size or 3.5
    chars = max(1, len(str(text)))
    along = chars * size * 0.72 + 4.0     # extent along the reading
    across = size * 1.9                   # extent across it
    if kind == "horizontal":
        return (x - along / 2.0, y - across * 0.5, x + along / 2.0, y + across * 1.6)
    return (x - across * 0.5, y - along / 2.0, x + across * 0.5, y + along / 2.0)


def nearest_vertical_edge(part, view, keys, target, profile):
    """The vertical outline edge whose u is nearest `target`.

    A dimension has to hang on real geometry, so a datum cannot simply be "the middle of
    the pattern": the closest straight edge to the pattern's centre is used instead, which
    on a plate or a flange is the edge through the part's own centre line.
    """
    if not keys:
        return None
    best = min(keys, key=lambda value: abs(float(value) - float(target)))
    return longest(profile["vertical"][best])


def nearest_horizontal_edge(part, view, keys, target, profile):
    """The horizontal outline edge whose v is nearest `target` (see above)."""
    if not keys:
        return None
    best = min(keys, key=lambda value: abs(float(value) - float(target)))
    return longest(profile["horizontal"][best])


def dimension_extent(part, tag, kind, value, size, x, y):
    """The real sheet rectangle a placed dimension occupies.

    `UF_DRF_ask_annotation_text_box` returns the text origin plus its width and height,
    so the extent is measured rather than estimated.  This is what makes the overlap test
    trustworthy: the estimate is a guess about font metrics, and it was letting readings
    print on top of each other because a rotated vertical reading is taller than it looks.
    """
    try:
        box = drf().UFSession.GetUFSession().Drf.AskAnnotationTextBox(tag)
        origin, width, height = box
        ox, oy = float(origin[0]), float(origin[1])
        # NX gives the text anchor; pad a little for the arrowheads and the gap.
        pad = 1.2
        if kind == "vertical":
            # A vertical reading is rotated, so its box is tall and narrow.
            return (ox - height * 0.5 - pad, oy - width * 0.5 - pad,
                    ox + height * 0.5 + pad, oy + width * 0.5 + pad)
        return (ox - pad, oy - height * 0.5 - pad,
                ox + width + pad, oy + height * 1.2 + pad)
    except Exception:
        return dim_text_box(value, size, x, y, kind)


def dim_line_box(left, bottom, right, top, kind, x, y, span=None, view=None):
    """Sheet rectangle the dimension LINE itself occupies, for crossing tests.

    A horizontal dimension runs across the gap it measures at height `y`; a vertical one
    runs up the side at `x`.  When `span` (the two view-space positions it measures) is
    known the box covers only that gap -- otherwise a two-hole spacing would be recorded
    as a full-width line and cross everything on the sheet.
    """
    if span and span[0] is not None and span[1] is not None and view is not None:
        try:
            a = view_sheet_point(view, span[0], span[1]) if kind == "horizontal" \
                else view_sheet_point(view, span[0], span[1])
            p0 = view_sheet_point(view, span[0], 0.0) if kind == "horizontal" \
                else view_sheet_point(view, 0.0, span[0])
            p1 = view_sheet_point(view, span[1], 0.0) if kind == "horizontal" \
                else view_sheet_point(view, 0.0, span[1])
            lo, hi = min(p0[0], p1[0]), max(p0[0], p1[0])
            lo_y, hi_y = min(p0[1], p1[1]), max(p0[1], p1[1])
            if kind == "horizontal":
                return (lo, y - 1.0, hi, y + 1.0)
            return (x - 1.0, lo_y, x + 1.0, hi_y)
        except Exception:
            pass
    if kind == "horizontal":
        return (left, y - 1.0, right, y + 1.0)
    return (x - 1.0, bottom, x + 1.0, top)


def cmd_dimension(args):
    """Real NX drafting dimensions, spread over the views and kept off every other line.

    Two rules drive the placement, both asked for explicitly:

      * a dimension must not pile up with the others -- each view offers four corridors
        (below, above, left, right) and a dimension goes to the least loaded one, so a
        complex part spreads its numbers all round the sheet instead of down one side;
      * it must not print on top of anything -- the candidate spot is tested against every
        view border and every dimension already placed, and moved outward until it is
        clear.

    Value text is NX's own measurement, so a dimension follows the model after an edit.
    """
    part = display_part()
    UFLIB = drf()
    wanted = args.get("views")
    if isinstance(wanted, str):
        wanted = [wanted]
    views = []
    available = drafting_views(part)
    if wanted:
        for name in wanted:
            views.append(pick_view(part, name))
    else:
        # Top shows length and width, Front shows thickness: the two that carry the
        # bulk of a plate's dimensions.  Other orthographic views are opt-in.
        for name in ("Top", "Front"):
            try:
                views.append(pick_view(part, name))
            except Exception:
                continue
        if not views:
            views = [v for v in available if is_orthographic(v)][:2]
    if not views:
        raise RuntimeError("the part has no drafting views yet — run nx_drawing first")
    size = args.get("textSize") or 3.5
    step = float(args.get("step") or 10.0)
    count_prefix = args.get("count")
    if count_prefix is None:
        count_prefix = True
    do_length = str(args.get("length") or "true").lower() not in ("false", "0", "no")
    do_diameter = str(args.get("diameter") or "true").lower() not in ("false", "0", "no")
    # A sheet that already carries dimensions must not get a second set on top: the same
    # reading then appears twice and every overlap test sees a crowd that is really its own
    # earlier output.  `replace` (the default) clears the old ones first; pass
    # replace:false to add to what is there.
    cleared = 0
    if str(args.get("replace") if args.get("replace") is not None else "true").lower() \
            not in ("false", "0", "no"):
        try:
            for dim in list(part.Dimensions):
                try:
                    UFLIB.UFSession.GetUFSession().Obj.DeleteObject(dim.Tag)
                    cleared += 1
                except Exception:
                    pass
        except Exception:
            pass
        if cleared:
            out("dimension: cleared %d existing dimension(s) first" % cleared)
    created = []
    seen_values = set()
    # Diameters are tracked by their measured size across the WHOLE sheet: the section view
    # repeats the outline's holes, and GB does not put the same Ø on a drawing twice.
    seen_diameters = set()
    # A Ø this view could not place is handed to the next view that can.  The set is shared
    # across the whole view loop, so a callout is never lost just because the view that
    # first saw it was crowded.
    deferred_diameters = set()
    skipped = []
    sheet = current_sheet(part)
    sheet_w, sheet_h = float(sheet.Length), float(sheet.Height)
    binding, margin = sheet_margins(sheet_w, sheet_h)
    limit_left = binding + 4.0
    limit_right = sheet_w - margin - 4.0
    limit_bottom = margin + 4.0
    limit_top = sheet_h - margin - 4.0
    # The GB title block sits in the bottom-right corner.  Dimensions must not be parked
    # on it: a run of readings laid across the title block prints unreadable text over the
    # material and signature cells.
    title_block = (sheet_w - margin - 180.0, margin, sheet_w - margin, margin + 56.0)

    # ---- every border on the sheet is an obstacle, not just the current view's.
    # The border is the outermost drawn line, so a dimension must stay clear of it: the
    # box is grown by a hair rather than shrunk, otherwise text lands on the outline.
    obstacles = []
    for other in drafting_views(part):
        try:
            b = UFLIB.UFSession.GetUFSession().Draw.AskViewBorders(other.Tag)
            obstacles.append((float(b[0]) - 1.5, float(b[1]) - 1.5,
                              float(b[2]) + 1.5, float(b[3]) + 1.5))
        except Exception:
            continue

    def free(box):
        """No view border, no title block, and no dimension already placed in this box."""
        if (box[0] < limit_left or box[2] > limit_right
                or box[1] < limit_bottom or box[3] > limit_top):
            return False
        if boxes_overlap(box, title_block):
            return False
        for other in obstacles:
            if boxes_overlap(box, other):
                return False
        return True

    # A corridor advances by the width of the text it just placed, not by a fixed step.
    # A fixed 0.75 * step advance (7.5 mm at the default step) is narrower than a four
    # character reading (~14 mm), so successive vertical dimensions printed on top of
    # each other -- exactly the overlap this is meant to prevent.
    def text_width(text, text_size):
        return len(str(text)) * (text_size or 3.5) * 0.72 + 4.0

    def advance(kind, text, text_size):
        """How far the next dimension in this corridor has to move out.

        Along a vertical corridor the readings are stacked, and each one is as tall as its
        own text is long (it is rotated), so the advance is the reading's length.  Along a
        horizontal corridor they are stacked flat, so the advance is one text height.
        """
        if kind == "vertical":
            return text_width(text, text_size) + 2.0
        return (text_size or 3.5) * 1.9 + 2.0

    # The corridors used to be measured only from the view border, so every run of the
    # same view started at the same distance and the second one landed on the first.
    # Tracking the outermost line per (view, side) keeps runs parallel instead.
    edge_used = {}
    # Dimension LINES already on the sheet.  Text boxes alone are not enough: a vertical
    # run parked left of the view still cuts across the horizontal run under it.
    line_boxes = []
    # The band each dimension claims, including the leader lines running back to the
    # geometry.  A chain of adjacent dimensions shares its reference points, so without
    # reserving the band the second reading is drawn on top of the first.
    bands = []

    def band_of(kind, x, y, view_left, view_bottom, view_right, view_top,
                span=None, view=None):
        """The strip a dimension needs, including the line it actually draws.

        The strip follows the dimension LINE's real extent, not the view width: NX draws
        the line between the two picked objects, and a line that reaches past the view edge
        (a chain whose last hole sits outside the outline) used to be recorded as narrow
        and then covered the neighbouring area.
        """
        reach = (size or 3.5) * 2.2
        if kind == "horizontal":
            lo, hi = view_left, view_right
            if span and span[0] is not None and span[1] is not None and view is not None:
                try:
                    p0 = view_sheet_point(view, span[0], 0.0)[0]
                    p1 = view_sheet_point(view, span[1], 0.0)[0]
                    lo, hi = min(lo, p0, p1), max(hi, p0, p1)
                except Exception:
                    pass
            return (lo, y - reach, hi, y + reach)
        lo_y, hi_y = view_bottom, view_top
        if span and span[0] is not None and span[1] is not None and view is not None:
            try:
                p0 = view_sheet_point(view, 0.0, span[0])[1]
                p1 = view_sheet_point(view, 0.0, span[1])[1]
                lo_y, hi_y = min(lo_y, p0, p1), max(hi_y, p0, p1)
            except Exception:
                pass
        return (x - reach, lo_y, x + reach, hi_y)

    for view in views:
        if not is_orthographic(view):
            out("view %s is not orthographic, skipped" % fix_text(view.Name))
            continue
        # A section is cut through the middle, so it repeats the outline's sizes at a
        # different scale and position; measuring them again there would put the same
        # numbers on the drawing twice.  It carries only what the other views cannot:
        # the depths and shoulders the cut exposes.
        is_section = type(view).__name__ == "SectionView"
        profile = ortho_profile(part, view)
        borders = UFLIB.UFSession.GetUFSession().Draw.AskViewBorders(view.Tag)
        left, bottom, right, top = (float(borders[0]), float(borders[1]),
                                    float(borders[2]), float(borders[3]))
        vertical = sorted(profile["vertical"])
        horizontal = sorted(profile["horizontal"])
        circles = profile["circles"]
        rims = profile.get("rims") or []
        # ---- what this view is FOR.
        #
        # "Spread the dimensions over the views" cannot be done by placing a reading
        # wherever a corridor happens to be free: the readings that are free to move are
        # exactly the ones that fit everywhere, and they all end up on the first view
        # visited.  A view is given the direction only it can measure, and the others are
        # left to the views that own them:
        #
        #   * the plan view (sight down Z) owns the X and Y positions of the holes;
        #   * a side view (sight down X) owns the Y positions and the depths Z;
        #   * the section owns the depths only -- its outline is the same part again.
        #
        # The overall size still goes wherever it can be measured without repeating the
        # other view's number, and a direction nobody else can show falls back to this view
        # rather than being dropped.
        sight = dominant_axis(view_normal(view.Matrix))
        u_axis = v_axis = None
        try:
            axes = view_axes(view)
            u_axis = dominant_axis(axes[0])
            v_axis = dominant_axis(axes[1])
        except Exception:
            pass
        # ---- which view owns a hole position along each model axis.
        #
        # A hole position along X reads in any view that does not look down X, so both Top
        # and Right can show it -- and both did, which is what put the same spacing on the
        # sheet twice or, once de-duplicated, crammed every Y reading onto Top.  Each axis
        # is assigned exactly one view: the one that draws it HORIZONTALLY (along u), since
        # a horizontal chain of spacings is what a drawing reads best; ties go to the view
        # visited first, which is the plan view.
        axis_owner = {}
        for candidate in views:
            if not is_orthographic(candidate):
                continue
            try:
                c_axes = view_axes(candidate)
                c_u = dominant_axis(c_axes[0])
                c_v = dominant_axis(c_axes[1])
            except Exception:
                continue
            # Horizontal first: it takes the axis only if nobody has claimed it as u yet.
            for model_axis, strength in ((c_u, 2), (c_v, 1)):
                if model_axis is None:
                    continue
                current = axis_owner.get(model_axis)
                if current is None or strength > current[0]:
                    axis_owner[model_axis] = (strength, fix_text(candidate.Name))
        owners = {axis: name for axis, (_, name) in axis_owner.items()}

        def owns(model_axis):
            """True when this view is the one that dimensions positions along `model_axis`."""
            if model_axis is None:
                return True
            name = axis_owner.get(model_axis, (0, fix_text(view.Name)))[1]
            return name == fix_text(view.Name)

        out("dimension: view %s sight %s, u=%s v=%s, owns u:%s v:%s"
            % (fix_text(view.Name), sight, u_axis, v_axis,
               owns(u_axis), owns(v_axis)))
        # Corridor load, so the next dimension goes to the emptiest side.  A corridor is
        # how far out its last line already sits.
        load = {"below": step, "above": step, "left": step, "right": step}
        # Bands already claimed by THIS view's dimensions.
        view_bands = []

        def corridor_spot(side, want, prefer=(), y_shift=0.0, span=None):
            """Walk outward along one corridor until the dimension fits and is clear.

            `prefer` lists the sides to try, in order; the first one that yields a free
            spot wins.  Nothing is placed when every side is full, and the caller reports
            the omission rather than printing a number over a line.

            Both the text box AND the dimension line are tested.  Text alone was not
            enough: a vertical run placed left of the view still crosses the horizontal
            run parked under it, and the two lines cut straight through the reading.

            `y_shift` slides a run along its corridor.  `span` is the pair of view-space
            positions the dimension measures: the line is then drawn across exactly that
            gap instead of across the whole view, which is what turned a two-hole spacing
            into a full-width line lying over everything else.
            """
            sides = []
            for name in tuple(prefer) + (side, "below", "above", "left", "right"):
                if name not in sides:
                    sides.append(name)
            # ---- prefer the corridor with the most ROOM left, not a fixed order.
            #
            # `sides` used to be tried in a hard-coded sequence, so everything that was not
            # explicitly preferred ended up below the view: 阀体's right-hand view printed
            # eighteen readings stacked under and beside it while the whole top half of the
            # sheet stayed empty, and 泵体's bottom two corridors ran into each other for
            # the same reason.  The usable room left in a corridor is how far the frame
            # still lets it grow; the sides are sorted by that, and the caller's `prefer`
            # list keeps its say as a tie-breaker only.
            def room_of(name):
                """Sheet space still free for this corridor, in mm.

                "below" and "left" grow towards the frame's bottom/left edges, so their
                room is the distance from the view border down to that limit; "above" and
                "right" grow the other way.  A corridor already used by earlier readings
                starts further out, so its remaining room is smaller.
                """
                key = (view.Name, name)
                already = edge_used.get(key, 0.0)
                used = max(load.get(name, 0.0), already)
                if name == "below":
                    return max(0.0, (bottom - limit_bottom) - used)
                if name == "above":
                    return max(0.0, (limit_top - top) - used)
                if name == "left":
                    return max(0.0, (left - limit_left) - used)
                return max(0.0, (limit_right - right) - used)

            preferred = set(prefer) | {side}
            # A preferred side wins only while it still has real room; once it is nearly
            # full the emptiest side takes over.  Ranking purely by preference is what
            # piled every reading below the view while the top half stayed blank.
            min_room = max(20.0, (size or 3.5) * 5.0)
            sides.sort(key=lambda name: (0 if (name in preferred
                                               and room_of(name) >= min_room) else 1,
                                         -room_of(name)))
            if span and span[0] is not None and span[1] is not None:
                mid = (float(span[0]) + float(span[1])) / 2.0
            else:
                mid = None
            for name in sides:
                key = (view.Name, name)
                distance = max(load[name], edge_used.get(key, 0.0) + step * 0.8)
                # How far a reading may be pushed out before the corridor is abandoned.
                #
                # The 40-step walk had no bound, so on a crowded sheet a reading kept
                # marching outward until it found blank paper -- 阀体's "1.5" and "4.2"
                # ended up 200 mm below the view with leaders crossing the whole sheet.
                # A dimension belongs beside its view: past about 45 mm the corridor is
                # given up and the caller records the omission.
                max_distance = max(45.0, (size or 3.5) * 12.0)
                for _ in range(40):
                    if distance > max_distance:
                        break
                    mid_x = (view_sheet_point(view, mid, 0.0)[0] if mid is not None
                             else (left + right) / 2.0)
                    mid_y = (view_sheet_point(view, 0.0, mid)[1] if mid is not None
                             else (bottom + top) / 2.0)
                    if name == "below":
                        x, y = mid_x, bottom - distance + y_shift
                    elif name == "above":
                        x, y = mid_x, top + distance + y_shift
                    elif name == "left":
                        x, y = left - distance + y_shift, mid_y
                    else:
                        x, y = right + distance + y_shift, mid_y
                    kind = "horizontal" if want == "h" else "vertical"
                    box = dim_text_box("000.00", size, x, y, kind)
                    line = dim_line_box(left, bottom, right, top, kind, x, y, span, view)
                    band = band_of(kind, x, y, left, bottom, right, top, span, view)
                    # The LINE has to clear the title block as well as the text: a chain
                    # measured across the part put a dimension line straight over the
                    # material and signature cells even though its reading sat outside.
                    if boxes_overlap(line, title_block):
                        distance += max(step * 0.45, (size or 3.5) * 1.2)
                        continue
                    # Bands are only checked against dimensions of the SAME view: two
                    # different views sit apart, and their leader bands legitimately reach
                    # into the gap between them.  Checking globally rejected every spot
                    # and left the drawing with a third of its dimensions.
                    if (free(box)
                            and not any(boxes_overlap(line, other) for other in line_boxes)
                            and not any(boxes_overlap(band, other)
                                        for other in view_bands)):
                        load[name] = distance + advance(kind, "000.00", size)
                        edge_used[key] = distance
                        return name, x, y, line, band
                    # Also step by the text size so a corridor never stalls: a fixed
                    # fraction of `step` can be narrower than the reading itself.
                    distance += max(step * 0.45, (size or 3.5) * 1.2)
            return None

        def place(kind, first, second, want, side, prefer=(), prefix=None,
                  allow_repeat=False, y_shift=0.0, span_a=None, span_b=None,
                  measure_axis=None):
            """Create the dimension at the first free spot along `side`.

            A reading of 0 means the two picks sit on the same line, so the number would
            be meaningless (it showed up on the pump body where a symmetric pair of holes
            shares one coordinate).  Such a dimension is created, checked and dropped.

            `allow_repeat` lets a hole pattern show +35 and -35 as two dimensions: both
            read "35", but they locate different holes, and suppressing the second would
            leave the pattern half dimensioned.

            `measure_axis` is what the number MEANS, and it is the de-duplication key.
            Keying on (kind, value) instead let the same size through twice: the pump body
            printed "90" under Top as a HORIZONTAL dimension and again beside the section as
            a VERTICAL one, and "35" appeared four times -- the two views measure the same
            edge of the same part, and a drawing may state a size once.  Only an axis that
            is genuinely a different direction (X vs Y vs Z) counts as a different size.

            After the dimension exists its TRUE extent is read back from NX and recorded,
            not the estimate.  The estimate was what let overlaps through: it is a guess
            about font metrics, while NX knows where the text actually went.
            """
            if measure_axis is None:
                measure_axis = kind
            spot = corridor_spot(side, want, prefer, y_shift, (span_a, span_b))
            if spot is None:
                skipped.append({"kind": kind, "reason": "no free spot on any corridor"})
                return False
            _, x, y, line, band = spot
            item = place_dimension(part, view, kind, first, second, x, y, prefix, size)
            value = item["value"]
            try:
                degenerate = abs(float(value)) < 1e-6
            except Exception:
                degenerate = False
            if degenerate:
                UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                skipped.append({"kind": kind, "value": value, "reason": "measures zero"})
                return False
            # The de-duplication key has to be the same for every view that measures the
            # same thing.  `measure_axis` arrives as (kind, model_axis) where model_axis is
            # 0/1/2 -- a MODEL axis, not the view's own u/v index -- so a spacing seen from
            # Top and the same spacing seen from a section both key on the same axis.
            # Where the caller had no axis to offer (a rim pair on a turned part), the
            # value alone is compared, which is the strictest reading and never lets a
            # duplicate through.
            key = (measure_axis if isinstance(measure_axis, tuple) else (measure_axis, None),
                   str(value))
            if not allow_repeat and key in seen_values:
                UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                skipped.append({"kind": kind, "value": value,
                                "reason": "same size already on the sheet"})
                return False
            # Read the real text box back; fall back to the estimate only if NX refuses.
            real = dimension_extent(part, item["tag"], kind, value, size, x, y)
            if any(boxes_overlap(real, other) for other in obstacles):
                UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                skipped.append({"kind": kind, "value": value,
                                "reason": "would overlap something already on the sheet"})
                return False
            # The title block is checked against the REAL text box too, not just the
            # estimate: 阀体 printed "1.5" and "4.2" across the material and signature
            # cells because the estimated box sat clear while the text NX actually drew
            # did not.
            if boxes_overlap(real, title_block):
                UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                skipped.append({"kind": kind, "value": value,
                                "reason": "would print on the title block"})
                return False
            seen_values.add(key)
            obstacles.append(real)
            line_boxes.append(line)
            bands.append(band)
            view_bands.append(band)
            created.append(item)
            return True

        def place_diameter_as_linear(view, radius, cu, cv, diameter, size, step_value):
            """Restate a circle's size as a linear dimension, when the geometry allows it.

            Returns None here: NX 2512 has no usable way to hang a linear dimension on the
            two quadrant points of ONE circle.  Every associativity that could name those
            points was tried and measured 0:

              * ARC_CENTER twice        -> both ends resolve to the same projected centre
              * END_POINT twice         -> a full circle's two vertices coincide
              * DWG_POS + a real edge   -> AssocDwgPos is ignored, still 0
              * SMART_POINT             -> "The first object associativity type is invalid"

            So the linear fallback cannot be built from a single circle.  The caller keeps
            the Ø callout and reports the omission instead; the real fix for a stack of
            concentric circles is to spread the callouts across two views (which
            minimal_views now guarantees by asking for at least two).
            """
            return None

        def edge_for_radius(view, radius, cu, cv):
            """The circle edge at (cu, cv) with this radius, for a linear dimension."""
            for (r, u, v), candidate in circles.items():
                if abs(float(r) - float(radius)) < 1e-6 \
                        and abs(float(u) - float(cu)) < 1e-6 \
                        and abs(float(v) - float(cv)) < 1e-6:
                    return candidate
            return None

        # ---- a SECTION view is dimensioned for depth, not for outline: the heights that
        # only the cut exposes (a shoulder, a cavity floor, a step) are exactly what the
        # other views cannot measure, so this is where they belong.
        if is_section and do_length and len(horizontal) >= 2:
            # Chained from the bottom, but only the significant steps: a cavity wall can
            # throw a dozen nearly-equal edges and a chain of all of them is unreadable.
            chain = [longest(profile["horizontal"][key]) for key in horizontal]
            steps = []
            for index in range(1, len(chain)):
                span = abs(float(horizontal[index]) - float(horizontal[index - 1]))
                if span >= max(1.0, (top - bottom) * 0.04):
                    steps.append((chain[index - 1], chain[index], span))
            steps.sort(key=lambda item: -item[2])
            for order_s, (a_edge, b_edge, span) in enumerate(steps[:4]):
                # Depth steps are their own sizes: they run along the view's vertical,
                # which on a section is the model axis the cut exposes.  Tagging them with
                # that axis keeps a depth that happens to equal an outline size distinct
                # from the outline size itself.
                place("vertical", a_edge, b_edge,
                      "v", "left" if order_s % 2 else "right",
                      ("left", "right"), None, False,
                      (order_s - 1.5) * step * 0.8,
                      measure_axis=("depth", v_axis))
        # ---- overall length, under the view.  Only the view that owns the horizontal
        # direction prints it: Top and Right both span 90 here, and printing the overall
        # size on both is the same number twice.
        if do_length and len(vertical) >= 2 and not is_section and owns(u_axis):
            place("horizontal",
                  longest(profile["vertical"][vertical[0]]),
                  longest(profile["vertical"][vertical[-1]]),
                  "h", "below", ("below",),
                  measure_axis=("size", u_axis))
        # ---- overall height, left of the view
        if do_length and len(horizontal) >= 2 and not is_section and owns(v_axis):
            place("vertical",
                  longest(profile["horizontal"][horizontal[0]]),
                  longest(profile["horizontal"][horizontal[-1]]),
                  "v", "left", ("left",),
                  measure_axis=("size", v_axis))
        # ---- every hole located in X and in Y, as a CHAIN of adjacent spacings.
        #
        # A fan from one datum edge is wrong on two counts.  It repeats the datum on every
        # reading (the arrows all land on the same line, which reads as "90 and 45 point at
        # the same thing"), and it is not how GB draws a hole pattern.  A chain gives each
        # gap once: edge -> hole1 -> hole2 -> ... -> edge, so no two dimensions share both
        # of their reference points.
        #
        # Concentric circles are one place (a bore and its counterbore), which is what the
        # (u, v) key already collapses.
        places = {}
        for (radius, cu, cv), edge in circles.items():
            places.setdefault((round(cu, 2), round(cv, 2)), edge)
        out("dimension: view %s, %d circle(s) at %d distinct place(s)"
            % (fix_text(view.Name), len(circles), len(places)))

        def chain_run(axis_keys, edges_at, datum_edge, kind, want, side, y_shifts,
                      model_axis=None):
            """Dimension consecutive features so every gap appears exactly once.

            A chain of neighbouring spacings is what GB draws, and it also fixes the
            "90 and 45 point at the same thing" complaint: a fan from one datum repeats
            that datum on every reading, so two dimensions end up sharing a reference.

            Only feature-to-feature gaps are chained.  The edge-to-first-hole step is left
            out: NX draws a dimension line across the whole distance between the two picked
            objects, so that one step spanned half the view and its line lay across
            everything.  The overall size dimension already fixes the edge, so the pattern
            is fully located without it.

            `allow_repeat` is NOT passed any more.  It was there so a symmetric pattern
            could show +35 and -35, but on the pump body that printed "35" FOUR times
            (twice under Top, twice beside the section): the four flange holes sit at
            (-35,-35), (-35,35), (35,-35), (35,35), so the X chain has one gap of 35 and
            the Y chain another, and each view measured both.  A hole pattern whose gaps
            repeat needs "4×35" -- one number with a count -- not the same number four
            times.  De-duplication therefore keys on (axis, gap) and the count is what
            carries the repetition.
            """
            stops = [edges_at[key] for key in axis_keys]
            positions = list(axis_keys)
            placed_count = 0
            for index in range(1, len(stops)):
                shift = y_shifts(placed_count)
                if place(kind, (stops[index - 1], UFLIB.DrfAssocType.ARC_CENTER),
                         (stops[index], UFLIB.DrfAssocType.ARC_CENTER),
                         want, side, (side, "above" if want == "h" else "right"),
                         None, False, shift, positions[index - 1], positions[index],
                         measure_axis=("pitch", model_axis)):
                    placed_count += 1
            return placed_count

        if do_length and places and vertical and not is_section and owns(u_axis):
            by_u = {}
            for (u_value, v_value), edge in sorted(places.items()):
                by_u.setdefault(u_value, edge)
            # Each step of the chain sits on its own line: the offset grows by a full
            # text height plus the arrowhead, so two neighbouring spacings can never
            # share a line or an arrowhead position.
            chain_run(sorted(by_u), by_u, longest(profile["vertical"][vertical[0]]),
                      "horizontal", "h", "below",
                      lambda n: -n * ((size or 3.5) * 2.4 + 3.0), u_axis)
        if do_length and places and horizontal and not is_section and owns(v_axis):
            by_v = {}
            for (u_value, v_value), edge in sorted(places.items()):
                by_v.setdefault(v_value, edge)
            # A vertical chain steps sideways the same way, so the rotated readings do not
            # line up shoulder to shoulder.
            chain_run(sorted(by_v), by_v, longest(profile["horizontal"][horizontal[0]]),
                      "vertical", "v", "left",
                      lambda n: -n * ((size or 3.5) * 2.4 + 3.0), v_axis)
        # ---- a turned part: no straight edges, so the length comes from the two rims.
        # Only for a part that has *only* rims: a pump body also carries plain circles,
        # and dimensioning a rim pair there measured 0 because the rims sat at one depth.
        if do_length and rims and not circles and not (vertical or horizontal):
            best = None
            for radius, centres, group in rims:
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        du = abs(centres[i][0] - centres[j][0])
                        dv = abs(centres[i][1] - centres[j][1])
                        if best is None or du + dv > best[0]:
                            best = (du + dv, du, dv, group[i], group[j])
            if best is not None and best[0] > 1e-6:
                _, du, dv, edge_a, edge_b = best
                if dv >= du:
                    place("vertical", (edge_a, UFLIB.DrfAssocType.ARC_CENTER),
                          (edge_b, UFLIB.DrfAssocType.ARC_CENTER), "v", "left",
                          ("left", "right"),
                          measure_axis=("size", v_axis))
                else:
                    place("horizontal", (edge_a, UFLIB.DrfAssocType.ARC_CENTER),
                          (edge_b, UFLIB.DrfAssocType.ARC_CENTER), "h", "below",
                          ("below", "above"),
                          measure_axis=("size", u_axis))
        # ---- one callout per distinct diameter, hung on its own circle.
        #
        # Two things had to be fixed here.  The readings used to be parked in a column 20 mm
        # outside the view, because `free()` refuses any box inside a view border -- and
        # every leader then crossed the whole view, five Ø numbers drawing five long lines
        # through the geometry.  A diameter belongs next to its own circle.
        #
        # But "next to its circle" is not enough on a part like the pump body, whose six
        # diameters are CONCENTRIC: six texts placed at their own rim + a gap all radiate
        # from one centre, and whichever direction each one happens to pick they end up in
        # the same few millimetres.  Concentric callouts are therefore given evenly spaced
        # directions (360/n apart) and the radius is grown until the box is clear, so they
        # fan out like the spokes of a wheel instead of piling up at the centre.
        if do_diameter and circles:
            by_size = {}
            for (radius, cu, cv), edge in sorted(circles.items()):
                by_size.setdefault(round(radius * 2.0, 3), []).append((radius, cu, cv, edge))
            drawn_scale = float(view.Scale)
            placed_text = []
            # ---- spread the callouts over the views.
            #
            # A stack of concentric circles cannot have its Ø callouts restated as linear
            # sizes (NX has no way to name two points of one circle -- see
            # place_diameter_as_linear), so the only lever is WHERE each callout lives.  Two
            # views can share the load, but sharing blindly is wrong in both directions:
            #
            #   * halving by index up front can starve a view that had plenty of room and
            #     overload one that had none, and it also drops sizes that would have fit;
            #   * every callout a view gives away has to be picked up somewhere else or it
            #     disappears from the drawing.
            #
            # So each view tries to place every size it can SEE, in order of how much room
            # its own circle offers, and only a size that finds no free spoke here is left
            # for a later view.  `deferred_diameters` carries those across the loop, and the
            # top-up pass at the end guarantees nothing is silently missing.
            pending = [(d, m) for d, m in sorted(by_size.items(), key=lambda pair: -pair[0])
                       if round(float(d), 3) not in seen_diameters]
            # A size already promised to an earlier view is that view's business.
            pending = [pair for pair in pending
                       if round(float(pair[0]), 3) not in deferred_diameters]
            total_pending = max(1, len(pending))
            for order_index, (diameter, members) in enumerate(pending):
                prefix = "%d\u00d7" % len(members) if (count_prefix and len(members) > 1) else None
                radius, cu, cv, edge = members[0]
                centre = view_sheet_point(view, cu, cv)
                drawn_r = radius * drawn_scale
                label = "%s\u00d8%g" % (prefix or "", diameter)
                # Even angular spacing: two callouts on one centre never share a spoke, so
                # their texts cannot print on top of each other however close the radii are
                # (the pump body's Ø66 and Ø68 differ by 1 mm).  If the preferred spoke is
                # blocked the next one is tried, rather than sliding further and further out
                # along a single direction.
                #
                # The step outward is measured from the RIM and kept short.  Walking out from
                # `drawn_r` by up to 100 mm is what gave 支撑板 a Ø26 leader reaching across
                # the sheet: a callout belongs beside its circle, a few millimetres away.
                # ---- which way the spoke points.
                #
                # The spokes used to start at 90 degrees and step round, which sent the
                # first callout straight up and the rest wherever the index happened to
                # land -- 阀体's Ø14.4, Ø8.5 and Ø3 all ended up on the same side, crossing
                # each other over the part while the space above the view stayed empty.
                #
                # Each spoke is scored by how much free sheet it leads into: the distance
                # from the circle's rim to the frame in that direction, minus whatever is
                # already occupied.  Callouts then fan towards open space instead of into
                # the crowd.  Even angular spacing is kept as the tie-breaker, so two
                # callouts on one centre still never share a spoke.
                def spoke_room(dx, dy):
                    room = float("inf")
                    for limit, delta, base in ((limit_right, dx, centre[0]),
                                               (limit_left, -dx, centre[0]),
                                               (limit_top, dy, centre[1]),
                                               (limit_bottom, -dy, centre[1])):
                        if delta > 1e-6:
                            room = min(room, (limit - base) / delta)
                    return room if room != float("inf") else 0.0

                base_angle = 90.0 - 360.0 * order_index / total_pending
                spoke_angles = []
                for spoke in range(0, 8):
                    deg = base_angle - spoke * 360.0 / 8.0
                    rad = math.radians(deg)
                    spoke_angles.append((deg, math.cos(rad), math.sin(rad)))
                # Rotate the ring so the roomiest direction comes first, keeping the 45
                # degree spacing of the rest.
                if spoke_angles:
                    best_start = max(range(len(spoke_angles)),
                                     key=lambda i: spoke_room(spoke_angles[i][1],
                                                              spoke_angles[i][2]))
                    spoke_angles = (spoke_angles[best_start:]
                                    + spoke_angles[:best_start])
                chosen_spot = None
                for _deg, dx, dy in spoke_angles:
                    for grow in range(0, 7):
                        reach = drawn_r + max(4.0, (size or 3.5) * 1.4) + grow * 3.0
                        spot_x = centre[0] + dx * reach
                        spot_y = centre[1] + dy * reach
                        # A Ø reading prints lying flat (it is not a rotated linear
                        # dimension), so its box is wide and short.
                        box = dim_text_box(label, size, spot_x, spot_y, "horizontal")
                        if boxes_overlap(box, title_block):
                            continue
                        # Test against EVERY dimension on the sheet, not only the ones this
                        # view placed: two views both calling out a Ø6 put their readings
                        # 1.6 pt apart (固定板 printed "6" on top of "6"), and a diameter
                        # sharing a box with a linear reading did the same on 导向轴套 and
                        # 泵体.  `obstacles` already carries every placed extent.
                        if any(boxes_overlap(box, other) for other in obstacles):
                            continue
                        if any(boxes_overlap(box, other) for other in placed_text):
                            continue
                        if not (limit_left <= spot_x <= limit_right
                                and limit_bottom <= spot_y <= limit_top):
                            continue
                        chosen_spot = (spot_x, spot_y)
                        break
                    if chosen_spot is not None:
                        break
                if chosen_spot is None:
                    # ---- no free spoke in THIS view: hand the size to a later view.
                    #
                    # The loop visits every view, so a size this one cannot place may well
                    # fit beside another.  Recording it in `deferred_diameters` keeps it
                    # available; the top-up pass at the end reports anything that never
                    # found a home.  That is how a stack of concentric circles gets spread
                    # over two or three views instead of piling up in one.
                    deferred_diameters.add(round(float(diameter), 3))
                    continue
                spot, y_want = chosen_spot
                item = place_dimension(part, view, "diameter",
                                       (edge, UFLIB.DrfAssocType.ARC_CENTER),
                                       None, spot, y_want, prefix, size)
                value = item["value"]
                # A Ø callout that reads 0 means the associativity did not take: drop it
                # rather than printing "Ø0" on the sheet (the pump folder's 偏心轴 came out
                # with two of them once the top-up pass ran).
                try:
                    if abs(float(str(value).replace("\u00d8", "").strip() or 0)) < 1e-6:
                        UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                        skipped.append({"kind": "diameter", "value": diameter,
                                        "reason": "association did not take (read 0)"})
                        continue
                except Exception:
                    pass
                # Diameters de-duplicate on their own namespace: a Ø20 and a linear 20 are
                # different statements about the part and must both appear.
                seen_values.add((("diameter", None), str(value)))
                try:
                    seen_diameters.add(round(float(str(value).replace("\u00d8", "").split("\u00d7")[-1]), 3))
                except Exception:
                    seen_diameters.add(round(float(diameter), 3))
                real = dimension_extent(part, item["tag"], "horizontal", value, size, spot, y_want)
                placed_text.append(real)
                obstacles.append(real)
                created.append(item)

    # ---- nothing may be left uncalled-out just because the views were sharing the work.
    #
    # The per-view loop hands a size it cannot place to `deferred_diameters`, expecting a
    # later view to pick it up.  A size that no view could take is retried here with a wider
    # search (any spoke, longer reach) before being reported in `skipped`.  Reporting an
    # omission is honest; a size silently missing from the drawing is not.
    if do_diameter and views:
        missing = []
        for view in views:
            if not is_orthographic(view):
                continue
            try:
                profile = ortho_profile(part, view)
            except Exception:
                continue
            for (radius, cu, cv), edge in sorted(profile["circles"].items()):
                size_value = round(float(radius) * 2.0, 3)
                if size_value not in seen_diameters:
                    missing.append((view, radius, cu, cv, size_value, edge))
        for view, radius, cu, cv, size_value, edge in missing:
            if size_value in seen_diameters:
                continue
            try:
                centre = view_sheet_point(view, cu, cv)
                drawn_r = float(radius) * float(view.Scale)
                label = "\u00d8%g" % size_value
                placed = False
                for spoke in range(8):
                    angle = math.radians(90.0 - spoke * 45.0)
                    dx, dy = math.cos(angle), math.sin(angle)
                    for grow in range(0, 9):
                        reach = drawn_r + max(5.0, (size or 3.5) * 1.6) + grow * 4.0
                        spot_x = centre[0] + dx * reach
                        spot_y = centre[1] + dy * reach
                        box = dim_text_box(label, size, spot_x, spot_y, "horizontal")
                        if boxes_overlap(box, title_block):
                            continue
                        if any(boxes_overlap(box, other) for other in obstacles):
                            continue
                        if not (limit_left <= spot_x <= limit_right
                                and limit_bottom <= spot_y <= limit_top):
                            continue
                        item = place_dimension(part, view, "diameter",
                                               (edge, UFLIB.DrfAssocType.ARC_CENTER),
                                               None, spot_x, spot_y, None, size)
                        # A callout that reads 0 is a bad association: delete it and let the
                        # next spoke be tried rather than printing "Ø0" on the sheet.
                        try:
                            bad = abs(float(item["value"])) < 1e-6
                        except Exception:
                            bad = False
                        if bad:
                            UFLIB.UFSession.GetUFSession().Obj.DeleteObject(item["tag"])
                            continue
                        seen_diameters.add(size_value)
                        seen_values.add((("diameter", None), str(item["value"])))
                        obstacles.append(dimension_extent(part, item["tag"], "horizontal",
                                                          item["value"], size,
                                                          spot_x, spot_y))
                        created.append(item)
                        placed = True
                        break
                    if placed:
                        break
                if not placed:
                    skipped.append({"kind": "diameter", "value": size_value,
                                    "reason": "no room for a callout on any view"})
            except Exception as error:
                out("diameter top-up failed:", repr(error))
    if not created:
        raise RuntimeError("nothing to dimension — the views carry no straight edges or circles")
    return {"dimensions": len(created), "views": [fix_text(v.Name) for v in views],
            "items": created, "skipped": skipped}


def hide_datums(sheet):
    """Blank the coordinate systems, datum axes and datum planes a view projects.

    These are modelling aids, not part geometry: they show up in every drafting view as
    long hairline crosses through its centre, and a printed sheet should not carry them.
    `Blank()` is NX's Hide switch on the object itself, so the part is untouched -- only
    the drawing stops drawing them.

    Returns {"count": n, "kinds": {...}} with what was actually hidden.
    """
    wanted = ("CartesianCoordinateSystem", "DatumAxis", "DatumPlane")
    found = {}
    for view in sheet.GetDraftingViews():
        try:
            visible = list(view.AskVisibleObjects())
        except Exception:
            continue
        for obj in visible:
            kind = type(obj).__name__
            if kind in wanted and obj.Tag not in found:
                found[obj.Tag] = (kind, obj)
    hidden = 0
    kinds = {}
    for tag, (kind, obj) in found.items():
        try:
            obj.Blank()
            hidden += 1
            kinds[kind] = kinds.get(kind, 0) + 1
        except Exception as error:
            out("could not blank %s %d: %r" % (kind, tag, error))
    return {"count": hidden, "kinds": kinds}


def cmd_drawing_frame(args):
    """A GB drawing frame plus title block, drawn as real sheet curves.

    A sheet made from an NX template prints its views but not the template
    frame, so the frame is drawn here as drafting curves + notes.
    """
    part = display_part()
    sheet = current_sheet(part)
    width = float(sheet.Length)
    height = float(sheet.Height)
    binding = float(args.get("binding") if args.get("binding") is not None else 25.0)
    margin = float(args.get("margin") if args.get("margin") is not None
                   else (10.0 if width > 500.0 else 5.0))
    tb_width = float(args.get("titleWidth") or 180.0)
    tb_height = float(args.get("titleHeight") or 56.0)
    if tb_width > width - margin - binding:
        tb_width = width - margin - binding
    if tb_height > height - margin * 2:
        tb_height = height - margin * 2
    part_name = str(args.get("partName") or fix_text(part.Leaf))
    company = str(args.get("company") or "")
    drawing_no = str(args.get("drawingNo") or "")
    material = str(args.get("material") or "45")
    designer = str(args.get("designer") or "")
    checker = str(args.get("checker") or "")
    date = str(args.get("date") or time.strftime("%Y-%m-%d"))
    try:
        raw = args.get("scale") or sheet.GetScale()
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            scale = "%g:%g" % (float(raw[0]), float(raw[1]))
        else:
            scale = str(raw)
    except Exception:
        scale = str(args.get("scale") or "1:1")

    # The title block labels are Chinese whatever the part is called, so the stroke font
    # has to be loaded even for a purely ASCII part name — otherwise every label prints
    # as a box in the PDF.
    cjk = True
    font_index = None
    if cjk:
        try:
            font_index = int(part.Fonts.AddFont("chinesef", NX.FontCollectionType.Nx))
        except Exception as error:
            out("cjk font not available:", repr(error))

    was_active = bool(sheet.IsActiveForSketching)
    if not was_active:
        sheet.ActivateForSketching()
    drawn = [0]

    def seg(x1, y1, x2, y2):
        part.Curves.CreateLine(pnt(x1, y1, 0.0), pnt(x2, y2, 0.0))
        drawn[0] += 1

    def box(x1, y1, x2, y2):
        seg(x1, y1, x2, y1)
        seg(x2, y1, x2, y2)
        seg(x2, y2, x1, y2)
        seg(x1, y2, x1, y1)

    box(0.0, 0.0, width, height)
    x0, y0 = binding, margin
    x1, y1 = width - margin, height - margin
    box(x0, y0, x1, y1)

    tb_x1, tb_y0 = x1, y0
    tb_x0 = tb_x1 - tb_width
    tb_y1 = tb_y0 + tb_height
    # GB/T 10609.1-2008 title block: laid out on the standard 180 x 56 grid and scaled to
    # whatever block size the sheet allows, so A4 and A2 keep the same proportions.
    # Columns: 更改区(0-90) | 材料/阶段/重量/比例(90-110) | 名称区(110-180)
    sx = tb_width / 180.0
    sy = tb_height / 56.0

    def X(value):
        return tb_x0 + value * sx

    def Y(value):
        return tb_y0 + value * sy

    def h(y, xa, xb):
        seg(X(xa), Y(y), X(xb), Y(y))

    def v(x, ya, yb):
        seg(X(x), Y(ya), X(x), Y(yb))

    box(tb_x0, tb_y0, tb_x1, tb_y1)

    # GB/T 10609.1 grid on the 180 x 56 block: change zone (4 rows x 7) above the signature
    # zone (4 rows x 7) on the left, material / stage / weight / scale in the middle, and the
    # name zone on the right with 18 / 21 / 9 / 8 mm rows.
    CHANGE_X = (0.0, 11.0, 22.0, 40.0, 58.0, 72.0, 90.0)   # 标记 处数 分区 更改文件号 签名 年.月.日
    MID_X = (90.0, 103.0, 110.0, 124.0)                    # 阶段标记 重量 比例
    NAME_X = 124.0
    for y in (28.0, 35.0, 42.0, 49.0):
        h(y, 0.0, 90.0)
    for x in CHANGE_X[1:-1]:
        v(x, 28.0, 56.0)
    for y in (7.0, 14.0, 21.0):
        h(y, 0.0, 90.0)
    for x in (22.0, 58.0):
        v(x, 0.0, 28.0)
    v(90.0, 0.0, 56.0)
    # material mark: 4 x 6.5 = 26 mm band from y = 30 up
    h(30.0, 90.0, NAME_X)
    for y in (36.5, 43.0, 49.5):
        h(y, 90.0, NAME_X)
    # stage mark / weight / scale row
    h(21.0, 90.0, NAME_X)
    for x in MID_X[1:-1]:
        v(x, 21.0, 30.0)
    v(NAME_X, 0.0, 56.0)
    # name zone rows: 单位名称 18 / 图样名称 21 / 图样代号 9 / 投影符号 8
    for y in (8.0, 17.0, 38.0):
        h(y, NAME_X, 180.0)

    written = [0]

    def label(text, x, y, size=3.0):
        """One drafting note, origin at grid point (x, y) of the 180 x 56 block."""
        text = str(text or "").strip()
        if not text:
            return
        note = part.Annotations.CreateDraftingNoteBuilder(None)
        note.Text.SetEditorText([text])
        if font_index is not None:
            try:
                note.Style.LetteringStyle.GeneralTextFont = font_index
            except Exception as error:
                out("title font not set:", repr(error))
        try:
            note.Style.LetteringStyle.GeneralTextSize = float(size)
        except Exception as error:
            out("title text size not set:", repr(error))
        note.Origin.OriginPoint = pnt(X(x) + 1.0 * sx, Y(y) + 1.0 * sy, 0.0)
        note.Commit()
        written[0] += 1

    # change zone headers
    label("标记", 1.0, 51.5, 2.5)
    label("处数", 12.0, 51.5, 2.5)
    label("分区", 23.0, 51.5, 2.5)
    label("更改文件号", 41.0, 51.5, 2.2)
    label("签名", 59.0, 51.5, 2.5)
    label("年.月.日", 73.0, 51.5, 2.2)
    # signature rows: 设计 (top) / 审核 / 工艺 / 批准 (bottom)
    for row, (role, who) in enumerate((("设计", designer), ("审核", checker),
                                       ("工艺", ""), ("批准", ""))):
        base = 21.0 - row * 7.0
        label(role, 1.0, base + 2.2, 2.5)
        label(who, 23.0, base + 2.2, 3.0)
        if row < 2:
            label(date, 59.0, base + 2.2, 2.2)
    # middle upper band: material mark (4 x 6.5 = 26 mm)
    label("材料标记", 91.0, 52.0, 2.2)
    label(material, 91.0, 44.8, 3.5)
    # middle row: stage mark / weight / scale, label on top and value below
    label("阶段标记", 91.0, 26.8, 2.0)
    label(str(args.get("stage") or ""), 91.0, 22.4, 2.5)
    label("重量", 104.0, 26.8, 2.0)
    label("比例", 111.0, 26.8, 2.0)
    label(scale, 111.0, 22.4, 2.4)
    # sheet count, centred in the lower middle band
    label("共 1 张  第 1 张", 91.0, 8.5, 2.2)
    # name zone: 单位名称 / 图样名称 / 图样代号 / 投影符号
    label("单位名称", 125.0, 49.0, 2.2)
    label(company, 125.0, 41.0, 3.2)
    label("图样名称", 125.0, 29.0, 2.2)
    label(part_name, 125.0, 20.5, 4.0)
    label("图样代号", 125.0, 10.8, 2.2)
    label(drawing_no, 125.0, 9.2, 3.0)
    label("投影符号", 125.0, 2.2, 2.2)

    if not was_active:
        sheet.ResetActiveForSketching()
    return {"sheet": fix_text(sheet.Name),
            "size": [round(width, 2), round(height, 2)],
            "curves": drawn[0], "texts": written[0],
            "titleBlock": [round(tb_width, 2), round(tb_height, 2)],
            "margins": {"binding": binding, "other": margin},
            "font": font_index}

# ------------------------------------------------------------------
# synchronous modelling: move / offset / pull / resize a face, replace or delete faces
# ------------------------------------------------------------------
SYNC_FACE_OPS = ("move", "offset", "pull", "resize")


def component_label(component):
    label = fix_text(getattr(component, "Name", "") or "")
    if label:
        return label
    try:
        return fix_text(getattr(component.Prototype, "Leaf", "") or str(component.Prototype)).upper()
    except Exception:
        return ""


def assembly_parts():
    """(assembly part, ComponentAssembly, positioner) of the displayed part."""
    part = display_part()
    assembly = part.ComponentAssembly
    return part, assembly, assembly.Positioner


def component_pool(assembly):
    children = []
    try:
        children = list(assembly.RootComponent.GetChildren())
    except Exception:
        pass
    return children


def component_bodies_in(part, budget=400):
    """Every body the assembly shows, as OCCURRENCES of the component prototypes.

    Nested sub-assemblies are walked to their leaves and suppressed components are
    skipped, so an exported mesh matches what the assembly actually displays.
    """
    assembly = getattr(part, "ComponentAssembly", None)
    rows = []
    if assembly is None:
        return rows
    fully_loaded.seen = 0
    fully_loaded.budget = budget

    def walk(component, depth=0):
        if depth > 12:
            return
        try:
            if component.IsSuppressed:
                return
        except Exception:
            pass
        try:
            kids = list(component.GetChildren())
        except Exception:
            kids = []
        if kids:
            for kid in kids:
                walk(kid, depth + 1)
            return
        try:
            prototypes = list(fully_loaded(component).Bodies)
        except Exception:
            return
        for body in prototypes:
            try:
                placed = component.FindOccurrence(body)
            except Exception:
                placed = None
            rows.append(placed if placed is not None else body)

    for child in component_pool(assembly):
        walk(child)
    return rows


def cmd_assembly_bom(args):
    """Bill of materials: quantities, per-part volume and mass, optional CSV.

    Suppressed components are left out unless includeSuppressed is set, the way
    an NX parts list treats them; the count of skipped ones comes back as
    totals.suppressed.
    """
    part, assembly, _ = assembly_parts()
    rollup = bom_rollup(assembly, bool(args.get("fast", False)),
                        bool(args.get("includeSuppressed", False)))
    suppressed = rollup["suppressed"]
    rows = rollup["components"]
    groups = rollup["groups"]
    totals = {
        "components": len(rows),
        "distinct": len(groups),
        "suppressed": suppressed,
        "volume": round(sum(float(row.get("volume") or 0.0) for row in groups), 4),
        "mass": round(sum(float(row.get("mass") or 0.0) for row in groups), 4),
    }
    result = {"assembly": fix_text(part.Leaf), "path": fix_text(part.FullPath),
              "groups": groups, "components": rows, "totals": totals}
    csv_path = args.get("csv")
    if csv_path:
        import csv
        target = csv_path if os.path.isabs(csv_path) else os.path.join(OUT_DIR, csv_path)
        folder = os.path.dirname(target)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "part file", "quantity", "bodies",
                             "volume_mm3", "mass_kg"])
            for row in groups:
                writer.writerow([row["name"], row["file"] or "", row["quantity"],
                                 row["bodies"], row["volume"], row["mass"]])
        result["csv"] = target
        result["csvBytes"] = os.path.getsize(target)
    return result


# ---------------------------------------------------------------- bom table & batch drafting


# ---------------------------------------------------------------- bom table & batch drafting
def bom_rollup(assembly, fast=False, include_suppressed=False):
    """Per-part rollup shared by nx_assembly_bom and the drawing table."""
    children = component_pool(assembly)
    fully_loaded.budget = max(fully_loaded.budget, len(children) + 8)
    suppressed = 0
    rows = []
    buckets = {}
    for component in children:
        try:
            if component.IsSuppressed:
                suppressed += 1
                if not include_suppressed:
                    continue
        except Exception:
            pass
        prototype = getattr(component, "Prototype", None)
        path = ""
        count = 0
        if prototype is not None:
            try:
                path = fix_text(prototype.FullPath) or ""
            except Exception:
                path = ""
            try:
                count = len(list(prototype.Bodies))
            except Exception:
                count = 0
        material = ""
        if prototype is not None:
            # GB 明细栏有「材料」列：读零件的 MATERIAL / 材料 属性，没有就留空。
            for attribute in ("MATERIAL", "材料", "Material"):
                try:
                    value = prototype.GetStringAttribute(attribute)
                except Exception:
                    value = None
                if value:
                    material = fix_text(value)
                    break
        row = {"name": component_label(component), "file": path or None,
               "bodies": count, "material": material}
        if not fast and prototype is not None:
            try:
                # A component of a big assembly is only partially loaded, so its
                # prototype reports no bodies and therefore no mass; pull it in
                # first or the BOM lists the workpiece as 0.0000 kg.
                stats = measure(fully_loaded(component) or prototype)
            except Exception:
                stats = {}
            for field in ("volume", "mass", "area"):
                if field in stats:
                    row[field] = round(float(stats[field]), 4)
            if (float(row.get("mass") or 0.0) <= 0.0
                    and float(row.get("volume") or 0.0) > 0.0):
                row["mass"] = round(float(row["volume"]) * STEEL_DENSITY, 4)
                row["massEstimated"] = True
        rows.append(row)
        key = os.path.normcase(path) or ("name:" + str(row["name"]))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"name": row["name"], "file": path or None, "quantity": 0,
                      "bodies": count, "volume": 0.0, "mass": 0.0}
            buckets[key] = bucket
        bucket["quantity"] += 1
        bucket["volume"] = round(bucket["volume"] + float(row.get("volume") or 0.0), 4)
        bucket["mass"] = round(bucket["mass"] + float(row.get("mass") or 0.0), 4)
        if row.get("massEstimated"):
            bucket["massEstimated"] = True
        # ---- keep the ASSEMBLY order, not a quantity sort.
        #
        # GB 明细栏 lists parts in the order they are assembled (or by part number), and a
        # drawing's item numbers must match that order so a balloon can point at "3" and
        # mean the third part fitted.  Sorting by (-quantity, name) put the twice-used parts
        # first and shuffled the rest alphabetically, which made the item numbers unrelated
        # to the assembly sequence.  The index below is the position of the part's FIRST
        # occurrence in the component tree.
        if "order" not in bucket:
            bucket["order"] = len(buckets)
    groups = sorted(buckets.values(), key=lambda row: row.get("order", 0))
    for index, row in enumerate(groups, 1):
        row["item"] = index
    return {"groups": groups, "components": rows, "suppressed": suppressed}

# Column catalogue: id -> (中文表头, English header, width mm). The panel lets the user tick
# which of these go on the sheet and switch the header language, so the table is not fixed to
# one layout any more.
# GB/T 10609.2-1989 明细栏：序号 / 代号 / 名称 / 数量 / 材料 / 单件重量 / 总计 / 备注,
# 列宽合计 180 mm，与 GB/T 10609.1 标题栏同宽。
BOM_FIELDS = {
    # The default six columns add up to 180 mm, the width of the GB title block, so the
    # table lines up with it instead of stopping short.
    "no": ("序号", "No.", 12.0),
    "part": ("代号", "Part No.", 30.0),
    "name": ("名称", "Name", 66.0),
    "qty": ("数量", "Qty", 12.0),
    "material": ("材料", "Material", 30.0),
    "unit": ("单件重量", "Unit wt", 24.0),
    "total": ("总计", "Total", 22.0),
    "note": ("备注", "Remarks", 30.0),
    "volume": ("体积 mm3", "Vol mm3", 28.0),
}
# The sheet does not report weights: the BOM lists identity, quantity and material only.
# `unit` / `total` / `volume` stay available through the columns argument and the CSV.
BOM_ORDER = ("no", "part", "name", "qty", "material", "note")
BOM_SUMS = ("qty", "unit", "total", "volume")


def bom_columns(spec, language):
    """Resolve the requested columns to (id, label, width) triples."""
    english = str(language or "zh").lower().startswith("en")
    if spec is None:
        ids = list(BOM_ORDER)
    elif isinstance(spec, str):
        ids = [item.strip() for item in spec.split(",") if item.strip()]
    else:
        ids = [str(item).strip() for item in spec if str(item).strip()]
    if not ids:
        ids = list(BOM_ORDER)
    unknown = [item for item in ids if item not in BOM_FIELDS]
    if unknown:
        raise RuntimeError("unknown BOM column(s) %s — available: %s"
                           % (", ".join(unknown), ", ".join(BOM_FIELDS)))
    columns = []
    for key in ids:
        zh, en, width = BOM_FIELDS[key]
        columns.append((key, en if english else zh, width))
    return columns

# Parts whose .prt carries no material come back with mass 0 and a real volume;
# a BOM that silently reports 0.0000 kg for the workpiece is worse than an
# estimate, so those rows fall back to steel and get marked in the table.
STEEL_DENSITY = 7.85e-6


def text_width_mm(text, size):
    """Rough advance width: CJK glyphs are square, capitals and digits wider.

    A flat 0.58 em for latin under-estimates upper case, and a long part number
    then runs across the next cell line (which is exactly what the fixture BOM
    did), so the classes are counted separately.
    """
    width = 0.0
    for char in str(text):
        if ord(char) > 0x2E80:
            width += size * 1.15
        elif char.isupper() or char.isdigit():
            width += size * 0.78
        elif char in " .,:;'`|!":
            width += size * 0.45
        else:
            width += size * 0.66
    return width


def fit_text(value, limit, size):
    text = str(value)
    if text_width_mm(text, size) <= limit:
        return text
    trimmed = text
    while trimmed and text_width_mm(trimmed + "..", size) > limit:
        trimmed = trimmed[:-1]
    return trimmed + ".." if trimmed != text else text


def sheet_line(part, x1, y1, x2, y2):
    return part.Curves.CreateLine(pnt(x1, y1, 0.0), pnt(x2, y2, 0.0))


def cell_text(part, text, x, y, size, font_index):
    builder = part.Annotations.CreateDraftingNoteBuilder(None)
    builder.Style.LetteringStyle.GeneralTextFont = int(font_index)
    try:
        builder.Style.LetteringStyle.GeneralTextSize = size
    except Exception:
        pass
    builder.Text.SetEditorText([text])
    builder.Origin.OriginPoint = pnt(x, y, 0.0)
    builder.Validate()
    return builder.Commit()


def clear_previous_bom(part):
    """Delete the table a previous `cmd_bom_table` drew on this sheet.

    The table is sheet curves plus one drafting note per cell, and NX keeps no handle to
    "the table", so drawing it twice printed two headers on one sheet.  The objects are
    found by geometry instead: the region the table occupied is recorded on the part as a
    user attribute, and every line/note lying inside it is deleted.  Without a recorded
    region nothing is deleted -- a first run has nothing to delete, and guessing could eat
    the frame or the title block.

    Notes are located with UF_OBJ_cycle_all + UF_OBJ_ask_type_and_subtype: the annotation
    collection on the part exposes no iterable for drafting notes, so this is the only way
    to reach them.
    """
    region = None
    try:
        text = part.GetStringUserAttribute("DSH_NX_BOM_REGION", 0)
        if text:
            left, bottom, right, top = [float(v) for v in str(text).split(",")]
            region = (left - 2.0, bottom - 2.0, right + 2.0, top + 2.0)
    except Exception:
        region = None
    if region is None:
        return 0

    def inside(x, y):
        return region[0] <= x <= region[2] and region[1] <= y <= region[3]

    uf = drf().UFSession.GetUFSession()
    removed = 0
    # ---- the ruled lines
    for curve in list(part.Curves):
        try:
            if type(curve).__name__ != "Line":
                continue
            start = curve.StartPoint
            end = curve.EndPoint
            if inside(start.X, start.Y) and inside(end.X, end.Y):
                uf.Obj.DeleteObject(curve.Tag)
                removed += 1
        except Exception:
            continue
    # ---- the cell texts (type 70 = annotation; subtype 2/3 are notes in this release)
    tags = []
    try:
        tag = uf.Obj.CycleAll(part.Tag, 0)
        guard = 0
        while tag != 0 and guard < 40000:
            try:
                major, _minor = uf.Obj.AskTypeAndSubtype(tag)
                if major == 70:
                    tags.append(tag)
            except Exception:
                pass
            tag = uf.Obj.CycleAll(part.Tag, tag)
            guard += 1
    except Exception as error:
        out("clear_previous_bom: cycling notes failed: %r" % (error,))
    for tag in tags:
        try:
            obj = part.FindObject(str(tag))
        except Exception:
            obj = None
        if obj is None:
            continue
        try:
            origin = obj.GetAnnotationOrigin() if hasattr(obj, "GetAnnotationOrigin") else None
        except Exception:
            origin = None
        if origin is None:
            try:
                origin = obj.Origin.OriginPoint
            except Exception:
                continue
        try:
            if inside(float(origin.X), float(origin.Y)):
                uf.Obj.DeleteObject(tag)
                removed += 1
        except Exception:
            continue
    return removed


def cjk_font(part):
    try:
        return int(part.Fonts.AddFont("chinesef", NX.FontCollectionType.Nx))
    except Exception:
        return None


def cmd_bom_table(args):
    """Draw the bill of materials as a ruled table on the current drawing sheet."""
    part, assembly, _ = assembly_parts()
    # The table goes on a sheet of the requested size; if the part only has A3 sheets and A2
    # was asked for, build the A2 instead of squeezing 19 rows onto the wrong paper.
    sheet_size = str(args.get("size") or "A3").upper()
    paper = SHEET_SIZES.get(sheet_size)

    def on_size(candidate):
        if paper is None:
            return True
        try:
            return (abs(float(candidate.Length) - paper[0]) < 1.0
                    and abs(float(candidate.Height) - paper[1]) < 1.0)
        except Exception:
            return True

    sheets = [sh for sh in list(part.DraftingDrawingSheets) if on_size(sh)]
    if not sheets:
        if args.get("drawing") is False:
            raise RuntimeError("this part has no %s drawing sheet — run nx_drawing first" % sheet_size)
        cmd_drawing({"size": sheet_size, "projection": "first",
                     "views": {"top": True, "front": True, "right": True}})
        sheets = [sh for sh in list(part.DraftingDrawingSheets) if on_size(sh)]
    if not sheets:
        sheets = list(part.DraftingDrawingSheets)
    sheet = None
    try:
        current = current_sheet(part)
    except Exception:
        current = None
    if current is not None and on_size(current):
        sheet = current
    for candidate in reversed(sheets):
        if sheet is None:
            sheet = candidate
    if sheet is None:
        raise RuntimeError("this part has no drawing sheet — run nx_drawing first")
    rollup = bom_rollup(assembly, bool(args.get("fast", False)),
                        bool(args.get("includeSuppressed", False)))
    groups = rollup["groups"]
    if not groups:
        raise RuntimeError("the assembly has no components to list")

    text_size = float(args.get("textSize") or 3.0)
    row_height = float(args.get("rowHeight") or 7.0)
    header_height = row_height + 1.5
    total_height = row_height + 1.0
    columns = bom_columns(args.get("columns"), args.get("language"))
    table_width = sum(width for _, _, width in columns)
    # Default position: right-aligned with the title block and hugging its top edge, so the
    # table sits directly above the title block instead of floating at the sheet corner.
    at = args.get("at")
    if at is None:
        _, margin = sheet_margins(float(sheet.Length), float(sheet.Height))
        at = [float(sheet.Length) - margin - table_width, margin + 56.0]
    x0, y0 = float(at[0]), float(at[1])
    data_rows = len(groups)
    header_bottom = y0 + total_height + data_rows * row_height
    table_top = header_bottom + header_height

    # A second call on the same sheet must REPLACE the table, not stack a second one on it.
    # NX offers no table object to delete, so the previous table's extent is remembered on
    # the part and everything inside it is removed first.
    try:
        removed = clear_previous_bom(part)
        if removed:
            out("bom_table: cleared the previous table (%d object(s))" % removed)
    except Exception as error:
        out("bom_table: clearing the previous table failed: %r" % (error,))
    try:
        # The region must cover the caption line too: it is drawn above `table_top`, so a
        # region ending at table_top left the old caption behind and two captions printed
        # on top of each other.
        part.SetUserAttribute("DSH_NX_BOM_REGION", 0,
                              "%f,%f,%f,%f" % (x0, y0,
                                               x0 + table_width,
                                               table_top + text_size * 2.0 + 8.0))
    except Exception as error:
        out("bom_table: could not record the table region: %r" % (error,))

    # grid: outer frame, one line per row boundary, one per column boundary
    tops = [y0, y0 + total_height] + [y0 + total_height + index * row_height
                                      for index in range(1, data_rows + 1)]
    tops += [table_top]
    pieces = []
    for value in tops:
        pieces.append(sheet_line(part, x0, value, x0 + table_width, value))
    edges = [x0]
    cursor = x0
    for _, _, width in columns:
        cursor += width
        edges.append(cursor)
    # The 合计 row spans the leading identity columns; when none of them is a numeric
    # summary column the row stays a normal row and every vertical runs the full height.
    merge_count = len(columns)
    for index, (key, _, _) in enumerate(columns):
        if key in BOM_SUMS:
            merge_count = index
            break
    for index, value in enumerate(edges):
        full = merge_count == 0 or index in (0, len(edges) - 1)
        low = y0 if full else y0 + total_height
        pieces.append(sheet_line(part, value, low, value, table_top))

    font = cjk_font(part)
    cells = 0

    def put(text, cell_x, cell_width, bottom, height):
        nonlocal cells
        if font is None:
            return
        # The CJK stroke font draws digits wider than text_width_mm assumes, so keep a
        # safety band and shrink the lettering until the label fits its own cell - a
        # 1.3951 in a 18 mm column used to run across the next column line.
        margin = min(6.0, max(2.5, cell_width * 0.09))
        avail = max(4.0, (cell_width - margin) * 0.92)
        size = text_size
        label = str(text)
        while size > 2.0 and text_width_mm(label, size) > avail:
            size = round(size - 0.2, 2)
        label = fit_text(label, avail, size)
        # A drafting note hangs BELOW its origin point, so centring a line in a
        # row means origin = bottom + (height + text size) / 2.
        try:
            cell_text(part, label, cell_x + 1.0, bottom + (height + size) * 0.5, size, font)
            cells += 1
        except Exception:
            # One cell refusing to draw must not lose the whole table.
            pass

    # header
    cursor = x0
    for (_, label, width) in columns:
        put(label, cursor, width, header_bottom, header_height)
        cursor += width

    # data rows, top-down: GB/T 10609.2 fills 序号 1 at the bottom, next to 合计
    total_mass = 0.0
    total_quantity = 0
    total_volume = 0.0
    for index, row in enumerate(groups):
        bottom = y0 + total_height + index * row_height
        number = index + 1
        leaf = os.path.splitext(os.path.basename(str(row.get("file") or "")))[0] or "-"
        quantity = int(row.get("quantity") or 0)
        mass = float(row.get("mass") or 0.0)
        total_mass += mass
        total_quantity += quantity
        volume = float(row.get("volume") or 0.0)
        total_volume += volume
        unit = mass / quantity if quantity else 0.0
        label = str(row.get("name") or "")
        # 代号列放文件名，名称列放中文名；两者相同时也照填（GB 明细栏两列都要有内容）
        mark = "*" if row.get("massEstimated") else ""
        part_file = os.path.basename(str(row.get("file") or "")) or leaf
        cells_by_id = {"no": str(number), "part": part_file, "name": label,
                       "qty": str(quantity),
                       "material": str(row.get("material") or ""),
                       "unit": "%.4f%s" % (unit, mark),
                       "total": "%.4f%s" % (mass, mark),
                       "note": str(row.get("note") or ""),
                       "volume": "%.1f" % volume}
        cursor = x0
        for (key, _, width) in columns:
            put(cells_by_id.get(key, ""), cursor, width, bottom, row_height)
            cursor += width

    # total row: label in the merged leading cell, sums under their own columns
    merge_label = args.get("totalLabel")
    if merge_label is None:
        merge_label = ("合计 / TOTAL" if not str(args.get("language") or "zh").lower().startswith("en")
                       else "TOTAL")
    merge_width = sum(width for _, _, width in columns[:merge_count]) if merge_count else 0.0
    if merge_count:
        put(str(merge_label), x0, merge_width, y0, total_height)
    sums_by_id = {"qty": str(total_quantity), "total": "%.4f" % total_mass,
                  "volume": "%.1f" % total_volume}
    cursor = x0 + merge_width
    for (key, _, width) in columns[merge_count:]:
        put(sums_by_id.get(key, ""), cursor, width, y0, total_height)
        cursor += width

    caption = args.get("caption")
    if caption is None:
        # No weight on the sheet: the title says what it is and how many items it lists.
        caption = "明细表  %s   %d 项 / %d 件" % (
            fix_text(part.Leaf), data_rows, total_quantity)
    if font is not None:
        # The note hangs below its origin, so lift it clear of the top border.
        cell_text(part, str(caption), x0, table_top + text_size + 4.0, text_size + 0.5, font)
    csv_path = args.get("csv")
    csv_info = {}
    if csv_path:
        # One button draws the table and drops the same numbers as a CSV: they come from
        # the same rollup, so the sheet and the spreadsheet cannot disagree.
        import csv as csv_module
        target = csv_path if os.path.isabs(csv_path) else os.path.join(OUT_DIR, csv_path)
        folder = os.path.dirname(target)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(["no", "part file", "name", "quantity", "unit_mass_kg",
                             "mass_kg", "volume_mm3"])
            for index, row in enumerate(groups, start=1):
                quantity = int(row.get("quantity") or 0)
                mass = float(row.get("mass") or 0.0)
                writer.writerow([index, row.get("file") or "", row.get("name") or "",
                                 quantity, round(mass / quantity, 4) if quantity else 0.0,
                                 round(mass, 4), round(float(row.get("volume") or 0.0), 4)])
        csv_info = {"csv": target, "csvBytes": os.path.getsize(target)}

    result = {
        "assembly": fix_text(part.Leaf),
        "sheet": fix_text(sheet.Name),
        "at": [round(x0, 2), round(y0, 2)],
        "columns": [label for _, label, _ in columns],
        "columnIds": [key for key, _, _ in columns],
        "language": "en" if str(args.get("language") or "zh").lower().startswith("en") else "zh",
        "rows": data_rows,
        "quantity": total_quantity,
        "mass": round(total_mass, 4),
        "table": {"width": round(table_width, 2),
                  "height": round(table_top - y0, 2),
                  "rect": [[round(x0, 2), round(y0, 2)],
                           [round(x0 + table_width, 2), round(table_top, 2)]]},
        "lines": len(pieces),
        "cells": cells,
        "suppressed": rollup["suppressed"],
    }
    result.update(csv_info)
    return result


def open_path(path):
    """Open a part (reusing a loaded one) and put it on display."""
    wanted = os.path.normcase(os.path.abspath(path))
    for loaded in list(s.Parts):
        try:
            if os.path.normcase(os.path.abspath(fix_text(loaded.FullPath))) == wanted:
                s.Parts.SetDisplay(loaded, True, True)
                return loaded
        except Exception:
            continue
    result = s.Parts.OpenDisplay(path)
    handle = result[0] if isinstance(result, (tuple, list)) else result
    s.Parts.SetDisplay(handle, True, True)
    return handle


def component_refs_of(path):
    """Assemblies (loaded in this session) that reference `path` as a component.

    Used by cmd_batch_drawings: part.SaveAs on a part an assembly points at rewrites
    that component's reference to the new file, and NXOpen offers no way to set it
    back (BasePart.FullPath is read-only). The fix is to put the part back afterwards:
    close the SaveAs'd copy and reopen the original path, which is what NX resolves the
    component against on load.
    """
    wanted = os.path.normcase(os.path.abspath(path))
    for loaded in list(s.Parts):
        try:
            assembly = getattr(loaded, "ComponentAssembly", None)
            if assembly is None:
                continue
            for component in component_pool(assembly):
                prototype = getattr(component, "Prototype", None)
                current = getattr(prototype, "FullPath", "") or ""
                if current and os.path.normcase(os.path.abspath(current)) == wanted:
                    return True
        except Exception:
            continue
    return False


def restore_component_reference(part, original):
    """Undo the reference rewrite a SaveAs on an assembly component causes.

    Verified on NX 2512: SaveAs moves the component to the copy; closing that copy and
    opening `original` again re-attaches the component to the real part. The copy in
    outDir stays on disk as a plain file, which is what the batch wants to deliver.
    """
    if not original or not os.path.exists(original):
        return False
    try:
        part.Close(NXOpen.BasePart.CloseWholeTree.TrueValue,
                   NXOpen.BasePart.CloseModified.DontCloseModified, None)
    except Exception as error:
        out("batch: could not close the copy after SaveAs: %r" % error)
        return False
    try:
        s.Parts.OpenActiveDisplay(original, NXOpen.DisplayPartOption.AllowAdditional)
    except Exception as error:
        out("batch: could not reopen %s after SaveAs: %r" % (original, error))
        return False
    return True


def cmd_batch_drawings(args):
    """A3 sheet + PDF for each part of an assembly, or for a list of files."""
    paths = []
    for raw in (args.get("paths") or []):
        paths.append(str(raw))
    if not paths and args.get("fromAssembly", True):
        part = display_part()
        children = component_pool(part.ComponentAssembly) if getattr(part, "ComponentAssembly", None) else []
        for component in children:
            prototype = getattr(component, "Prototype", None)
            if prototype is None:
                continue
            try:
                path = fix_text(prototype.FullPath) or ""
            except Exception:
                path = ""
            if path and os.path.exists(path):
                paths.append(path)
    seen = []
    unique = []
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.append(key)
        unique.append(path)
    if not unique:
        active = ""
        try:
            active = fix_text(display_part().Leaf) or ""
        except Exception:
            active = ""
        raise RuntimeError(
            "nothing to draw: the active part%s is not an assembly with components on disk. "
            "Open the assembly first (nx_open on the .prt), or pass paths=[...] with the "
            "part files to draw." % (" (%s)" % active if active else ""))
    # 图纸顺序：调用方给了 order（文件名或路径列表）就按它排，
    # 没给的排到后面保持装配原顺序。默认就是装配顺序。
    order = [str(item) for item in (args.get("order") or []) if str(item).strip()]
    if order:
        def rank(path):
            base = os.path.splitext(os.path.basename(path))[0]
            for index, wanted in enumerate(order):
                target = os.path.splitext(os.path.basename(str(wanted)))[0]
                if base == target or str(wanted) == path:
                    return index
            return len(order) + unique.index(path)
        unique = sorted(unique, key=rank)
    # 图号：装配图固定 0，零件按当前顺序 1..n（可加前缀、可补零）
    number_prefix = str(args.get("numberPrefix") or "")
    number_start = int(args.get("numberStart") or 1)
    number_digits = int(args.get("numberDigits") or 0)

    def drawing_no_for(index, is_assembly=False):
        number = 0 if is_assembly else (number_start + index)
        text = str(number)
        if number_digits > 0:
            text = text.zfill(number_digits)
        return number_prefix + text

    out_dir = args.get("outDir") or os.path.join(OUT_DIR, "batch")
    out_dir = out_dir if os.path.isabs(out_dir) else os.path.join(OUT_DIR, out_dir)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # Batch drawings are part drawings, so they default to A4.
    size = str(args.get("size") or "A4").upper()
    # Views: let cmd_drawing choose the minimal set unless the caller names one.
    #
    # This used to force top+front+right on every part, so a plain plate got a third view
    # that carried no dimension at all (the panel's 固定板 came out with an empty rectangle
    # on the right).  cmd_drawing's minimal_views() picks the fewest views that still let
    # every size be dimensioned, which is what a batch of mixed parts needs.
    views = args.get("views")
    reuse = args.get("reuse", True) is not False
    # 尺寸分开控制：零件图默认都标，调用方可以单独关掉线性或直径
    dim_length = args.get("length", True) is not False
    dim_diameter = args.get("diameter", True) is not False
    material = args.get("material") or ""
    keep = state.get("part")
    rows = []
    used_targets = set()

    def pdf_target(path):
        """A pdf file name for this part, Chinese kept and duplicates numbered.

        The old name went through a keep-only-[A-Za-z0-9_.-] filter, so every Chinese part
        name collapsed into "_" and a 11-part assembly printed eleven drawings into one
        _.pdf: the panel reported "11/11 printed" while the folder held a single file.
        """
        base = os.path.splitext(os.path.basename(path))[0]
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", base).strip(" .") or "part"
        candidate = os.path.join(out_dir, safe + ".pdf")
        key = os.path.normcase(candidate)
        if key in used_targets:
            index = 2
            while os.path.normcase(os.path.join(out_dir, "%s_%d.pdf" % (safe, index))) in used_targets:
                index += 1
            candidate = os.path.join(out_dir, "%s_%d.pdf" % (safe, index))
            key = os.path.normcase(candidate)
        used_targets.add(key)
        return candidate



    # 先出装配图（图号 0）。装配图要放明细表，所以默认用 A3，并画上 BOM 表。
    assembly_row = None
    if args.get("assemblyDrawing", True) is not False:
        assembly_path = ""
        try:
            active = display_part()
            if getattr(active, "ComponentAssembly", None) is not None:
                assembly_path = fix_text(active.FullPath) or ""
        except Exception:
            assembly_path = ""
        if assembly_path and os.path.exists(assembly_path):
            assembly_entry = {"path": assembly_path, "part": os.path.splitext(
                os.path.basename(assembly_path))[0], "drawingNo": drawing_no_for(0, True)}
            started = time.time()
            try:
                open_path(assembly_path)
                state["part"] = display_part()
                assembly_size = str(args.get("assemblySize") or "A3").upper()
                drawing_args = {"size": assembly_size,
                                "projection": args.get("projection") or "first",
                                "autoScale": True,
                                "drawingNo": assembly_entry["drawingNo"]}
                if material:
                    drawing_args["material"] = material
                cmd_drawing(drawing_args)
                assembly_entry["created"] = True
                # 装配图配明细表，否则装配图只有视图没有零件清单
                try:
                    cmd_bom_table({"language": args.get("language") or "zh"})
                    assembly_entry["bom"] = True
                except Exception as error:
                    assembly_entry["bomError"] = "%s: %s" % (type(error).__name__, error)
                target = pdf_target(assembly_path)
                printed = cmd_pdf({"path": target})
                assembly_entry["pdf"] = printed["path"]
                assembly_entry["pdfBytes"] = printed["bytes"]
                assembly_entry["sheet"] = sheet_info(current_sheet(display_part()))
            except Exception as error:
                assembly_entry["error"] = "%s: %s" % (type(error).__name__, error)
            assembly_entry["seconds"] = round(time.time() - started, 1)
            rows.append(assembly_entry)
            assembly_row = assembly_entry
    for position, path in enumerate(unique):
        entry = {"path": path}
        entry["drawingNo"] = drawing_no_for(position)
        started_at = time.time()
        referenced = False
        original_path = path
        part = None
        try:
            referenced = component_refs_of(path)
            if referenced:
                entry["assemblyComponent"] = True
            part = open_path(path)
            state["part"] = part
            entry["part"] = fix_text(part.Leaf)
            sheets = list(part.DraftingDrawingSheets)
            # Reuse only a sheet of the size that was asked for.  A part that already
            # carried an A4 sheet printed as A4 even though the batch asked for A3 (阀体1
            # in the panel's folder), and the caller had no way to tell: the row said
            # "Sheet 1" either way.  The size is now part of the decision.
            wrong_size = False
            if sheets and reuse:
                paper = SHEET_SIZES.get(size)
                if paper is not None:
                    for existing in sheets:
                        try:
                            same = (abs(float(existing.Length) - paper[0]) < 1.0
                                    and abs(float(existing.Height) - paper[1]) < 1.0)
                        except Exception:
                            same = True
                        if same:
                            break
                    else:
                        wrong_size = True
            if not sheets or not reuse or wrong_size:
                drawing_args = {"size": size, "projection": args.get("projection") or "first",
                                "views": views, "autoScale": True}
                if material:
                    drawing_args["material"] = material
                drawing_args["drawingNo"] = entry["drawingNo"]
                cmd_drawing(drawing_args)
                entry["created"] = True
                if wrong_size:
                    entry["note"] = "existing sheet was not %s, a new one was made" % size
            else:
                entry["created"] = False
            # A part whose sheet exists but is not the ACTIVE one hands back None here, and
            # the whole part then failed with "'NoneType' object has no attribute 'Name'"
            # (阀体1 in the panel's batch folder): the sheet was on disk, NX just had not
            # made it current after opening the part.  Activate it instead of giving up.
            sheet = current_sheet(part)
            if sheet is None:
                sheets = list(part.DraftingDrawingSheets)
                if sheets:
                    sheet = sheets[0]
                    try:
                        sheet.ActivateForSketching()
                    except Exception as error:
                        out("batch: could not activate %s: %r"
                            % (fix_text(sheet.Name), error))
                    entry["activated"] = fix_text(sheet.Name)
            if sheet is None:
                raise RuntimeError("the part has no drawing sheet to print")
            entry["sheet"] = sheet_info(sheet)
            # ---- dimension the sheet before printing it.
            #
            # Batch drawing used to stop at "views + frame + PDF", so every sheet it
            # produced was an undimensioned picture: on the panel's 12-part folder every
            # drawing came out with three stray readings and nothing else, which is not a
            # drawing anyone can make a part from.  The dimension pass runs over the views
            # that are actually on this sheet, and a failure is reported per part instead
            # of aborting the batch -- a part whose geometry carries no dimensionable
            # feature still deserves its views and frame.
            if args.get("dimension", True) is not False:
                try:
                    # cmd_dimension clears any existing dimensions itself (replace defaults
                    # to true), so a reused sheet gets one fresh set rather than a second
                    # one on top of the old.
                    view_names = []
                    for view in sheet.GetDraftingViews():
                        name = fix_text(view.Name)
                        try:
                            shown = view_summary(view)["visible"]
                        except Exception as error:
                            out("batch: %s: %r" % (name, error))
                            continue
                        # None means NX would not say (sheet never displayed); such a
                        # view cannot be dimensioned, but it must not abort the part.
                        if shown:
                            view_names.append(name)
                    dim_report = cmd_dimension({"views": view_names,
                                                "length": dim_length,
                                                "diameter": dim_diameter})
                    entry["dimensions"] = dim_report.get("dimensions", 0)
                    entry["dimensionSkipped"] = len(dim_report.get("skipped") or [])
                except Exception as error:
                    entry["dimensionError"] = "%s: %s" % (type(error).__name__, error)
                    out("batch: dimension failed for %s: %r"
                        % (entry.get("part"), error))
            target = pdf_target(path)
            printed = cmd_pdf({"path": target})
            entry["pdf"] = printed["path"]
            entry["pdfBytes"] = printed["bytes"]
            if not printed.get("exists") or not os.path.exists(target) or os.path.getsize(target) < 1000:
                raise RuntimeError("the PDF was not written (%s bytes)" %
                                   printed.get("bytes", 0))
            # The PDF is only a print; the drawing itself has to come back as a .prt so it
            # can still be edited in NX. SaveAs keeps the source part untouched and writes
            # the drawing, the views and the frame into the copy in outDir.
            model_target = os.path.splitext(target)[0] + ".prt"
            entry["model"] = model_target
            try:
                # Never delete the part we were asked to draw. outDir is usually a fresh
                # folder, but a caller may point it at the source directory, and then
                # model_target IS the source file: removing it first would destroy the
                # model instead of writing a drawing next to it.
                same_as_source = os.path.normcase(os.path.abspath(model_target)) == \
                    os.path.normcase(os.path.abspath(original_path))
                if same_as_source:
                    raise RuntimeError(
                        "outDir resolves to the part's own folder (%s); drawing there would "
                        "overwrite the source part - use a separate outDir" % model_target)
                if os.path.exists(model_target):
                    os.remove(model_target)
                part.SaveAs(model_target)
                entry["modelBytes"] = os.path.getsize(model_target)
            except Exception as error:
                entry["model"] = None
                entry["modelError"] = "%s: %s" % (type(error).__name__, error)
        except Exception as error:
            entry["error"] = "%s: %s" % (type(error).__name__, error)
        finally:
            # SaveAs above moved this part's file name; if an assembly pointed at it,
            # that assembly now references the copy in outDir. Put the real part back.
            if referenced and part is not None and entry.get("model"):
                if restore_component_reference(part, original_path):
                    entry["referenceRestored"] = original_path
        entry["seconds"] = round(time.time() - started_at, 1)
        rows.append(entry)
    if keep is not None:
        try:
            s.Parts.SetDisplay(keep, True, True)
            state["part"] = keep
        except Exception:
            pass
    ok = [row for row in rows if "pdf" in row and not row.get("error")]
    files = []
    models = []
    for row in ok:
        pdf_path = row.get("pdf")
        if pdf_path and os.path.exists(pdf_path):
            files.append(pdf_path)
        model_path = row.get("model")
        if model_path and os.path.exists(model_path):
            models.append(model_path)
    return {
        "outDir": out_dir,
        "parts": len(unique),
        # 装配图单独计数：drawn 只算零件，避免 "11/11" 变成 "12/11" 这种错位。
        "assembly": ({"drawingNo": assembly_row.get("drawingNo"),
                      "pdf": assembly_row.get("pdf"),
                      "pdfBytes": assembly_row.get("pdfBytes"),
                      "error": assembly_row.get("error")}
                     if assembly_row else None),
        "drawn": len([row for row in ok if row is not assembly_row]),
        # What is actually on disk, so "11/11 printed" can be checked against the folder.
        "files": files,
        "models": models,
        "dimensions": sum(row.get("dimensions") or 0 for row in ok),
        "undimensioned": [row.get("part") for row in ok if not row.get("dimensions")],
        "failed": [row for row in rows if row.get("error")],
        "rows": rows,
    }

LAYER_LABELS = ("Hidden", "Selectable", "Visible", "WorkLayer")

