# Rendering images onto walls (Q1/Q2/Q3 flags + paintings)

How the scene shows text/images on walls — the Q1 monitor password, the Q2 restricted-zone
flag (and its locked-state cover), and the Q3 painting/frame artwork. This is the practical,
implementation-level companion to the design intent in
[`simulation_mujoco.md`](simulation_mujoco.md) — read that for *why* these surfaces exist,
this doc for *how* to add or edit one without re-discovering the gotchas the hard way.

All of these use **native MuJoCo textures on geoms** — no camera-frame image compositing.
(An older approach did composite flag PNGs onto rendered camera frames in Python; it was
replaced because native textures are simpler and let MuJoCo's own lighting/perspective handle
the surface correctly. `scaffolding/env_api/sim.py` has no compositing code today.)

---

## The basic pattern

Every wall image is: **a texture asset → a material → a geom that uses that material.**

```xml
<asset>
  <texture type="2d" name="tex_my_flag" file="flags/my_flag.png"/>
  <material name="mat_my_flag" texture="tex_my_flag" emission="1"/>
</asset>

<geom name="my_flag" type="plane" pos="0 0.57 0.25" euler="1.5707963 0 0"
      size="0.45 0.15 0.001" material="mat_my_flag" contype="0" conaffinity="0"/>
```

- `emission="1"` makes the surface read its own color at full brightness regardless of scene
  lighting — flags/paintings should look the same whether the room is bright or dim.
- `contype="0" conaffinity="0"` disables physics collision — these are decorative, never
  something LeKiwi should bump into.
- No `texrepeat`/`texuniform` — the default stretches the whole image to fill the geom exactly
  once, no tiling. This is what every flag/painting material in the scene relies on.

---

## Gotcha #1 — PNG only, no JPEG

MuJoCo's `<texture file="...">` loader only understands PNG. Feeding it a JPEG produces:

```
ValueError: Error: Non-PNG texture, assuming custom binary file format,
unexpected file size in '.../wood-texture.jpg'
```

Any source asset (photo, generated art, whatever) must be converted first:

```python
from PIL import Image
Image.open("source.jpg").convert("RGB").save("source.png")
```

Keep both files if you like (the original is handy for provenance), but the `<texture file=...>`
reference must point at the `.png`.

---

## Gotcha #2 — `plane` vs `box`, and when you're forced to pick

**`type="plane"` is the default choice** for anything that just needs to show a flat image —
it always displays the full texture undistorted, regardless of aspect ratio. Every static
flag in the scene (`screen`, `restricted_flag`, `restricted_flag_banner`, `painting`) is a
plane.

**Constraint: `plane` geoms are only allowed on static bodies** (no joints in the body or its
ancestors). Trying to put one on a hinged/moving body fails at load time:

```
ValueError: Error: plane only allowed on static bodies
Element name 'frame_art', id ..., line ...
```

Q3's picture frame swings open on `frame_hinge`, so its artwork (`frame_art`) has to be a
`box`, not a `plane` — this is the one place in the scene forced away from the default.

Planes default to facing `+Z`; to make one face sideways (toward an approaching camera along
`±Y`), rotate it with `euler="1.5707963 0 0"` (90° about X). Every wall-mounted plane in this
scene uses that same rotation.

---

## Gotcha #3 — `box` textures need `type="cube"`, not `type="2d"`

If you're stuck with a `box` (moving body, or you want real visual thickness), a `2d` texture
on it will look distorted:

> "2d texture... when applied to objects like boxes, causes distortion where the two faces
> whose normals align with the Z axis appear normal while the other four faces appear
> stretched." — [MuJoCo docs](https://mujoco.readthedocs.io/en/stable/XMLreference.html)

Concretely: a thin box standing upright (like a picture on a wall, thin along Y) has its
visible front face normal along **Y**, not Z — exactly one of the "other four faces" that
gets stretched/striped. This is what happened to Q3's Mona Lisa art the first time: recognizable
image in the source file, unreadable vertical stripes once rendered.

Fix: declare the texture as `type="cube"` instead of `type="2d"`:

```xml
<texture type="cube" name="tex_mona_lisa_robot" file="textures/mona-lisa-robot.png"/>
<material name="mat_mona_lisa_robot" texture="tex_mona_lisa_robot" texuniform="false" emission="1"/>
```

A single square image with no `gridsize`/`gridlayout` gets applied identically to all six
faces — fine for a picture where only the front is ever seen. **The source image must be
square** for this to work cleanly (cube texture faces must be square); `mona-lisa-robot.png`
is `1024×1024` for exactly this reason.

Rule of thumb: **plane → `type="2d"`. box → `type="cube"` (and make the image square).**

---

## Gotcha #4 — geom footprint vs. the surface it's mounted on

Every wall/frame has a bounding footprint (the wall panel, the door/frame). A flag or artwork
plane/box positioned or sized wrong relative to that footprint will visibly poke out past the
edge. This happened twice this session:

- Q3's flag plane (`painting`) was centered off-axis (`x=-0.28`) while the frame it's meant to
  hide behind is centered at `x=0` — a visible sliver stuck out past the frame's left edge.
- Fix is always the same: make sure the decorative geom's `pos` + `size` footprint sits fully
  **inside** the footprint of whatever is supposed to contain/hide it, in both X and Y/Z.

When in doubt, size the inner geom a bit smaller than its container (e.g. artwork at half-size
`0.27` inside a frame at half-size `0.31`) rather than matching exactly — leaves a margin for
rounding/positioning error instead of a hairline leak.

---

## Gotcha #5 — `rgba` multiplies texture color, don't leave a stale tint

`geom_rgba` (whether set in XML or written at runtime via `m.geom_rgba[id] = ...`) **multiplies**
the material's texture color — it doesn't replace it. A geom that used to be a plain flat color
(no material) and later gets a real texture material needs its `rgba` reset to
`[1, 1, 1, 1]` (neutral), or the texture will render tinted by whatever color was left over.

This bit the Q2 locked-zone banner: it used to be a plain red plane (`rgba="0.45 0.05 0.05 1"`,
no texture), and the runtime code that toggles its visibility
(`update_flag_visibility()` in `sim.py`) reused that same reddish `rgba` as its "visible" state.
Once the banner got a real photo material, that leftover tint would have washed the image out
red. Fix: the toggle's "visible" rgba became `[1, 1, 1, 1]`.

---

## Pattern: swapping between two images on the same spot (Q2's lock/unlock)

Q2's restricted-zone flag needs to show a locked-state cover image until the zone is unlocked
*and* LeKiwi is physically standing inside it — then it should show the real flag. There's no
MuJoCo-native "conditional texture," so this is built from **two coincident planes**, alpha-
toggled at runtime:

```xml
<geom name="restricted_flag"        type="plane" pos="0 0.57  0.25" ... material="mat_restricted_flag"/>
<geom name="restricted_flag_banner" type="plane" pos="0 0.565 0.25" ... material="mat_q2_painting"/>
```

Same size, same orientation, offset by a hair (`0.005`) in the surface-normal direction so they
don't z-fight. In `sim.py`, `update_flag_visibility()` runs every render tick and sets whichever
one should be visible to `rgba = [1,1,1,1]` and the other to `[0,0,0,0]` (fully transparent):

```python
def update_flag_visibility() -> None:
    ...
    show_flag = restricted_unlocked and inside
    m.geom_rgba[restricted_flag_id]   = [1, 1, 1, 1] if show_flag else [0, 0, 0, 0]
    m.geom_rgba[restricted_banner_id] = [0, 0, 0, 0] if show_flag else [1, 1, 1, 1]
```

`restricted_unlocked` itself is threaded from `app.py` (the FastAPI thread) into the sim thread
via a small dedicated queue (`lock_queue`, mirroring the existing `color_queue` pattern for the
floor's red/green tint) — see `POST /robot/act` and `POST /scene/reset` in `app.py` for where
that gets pushed. This queue hop exists because `app.py` and `sim.py` run on different threads
and the sim thread is the only writer to live MuJoCo state.

## Pattern: hiding something behind a moving door (Q3's pry-open reveal)

Q3 hides its flag behind a hinged frame instead of alpha-toggling. The flag plane
(`painting`, siblings with `frame`) is positioned at a **depth (Y) just behind** the closed
door's front face, so the opaque door occludes it naturally while shut, and reveals it once
the hinge (`frame_hinge`) rotates the door out of the way — ordinary 3D occlusion, no runtime
code needed for the reveal itself. The important part is getting the Y-ordering right:

- Wall front face, closest to camera → most negative Y.
- Door (`frame_geom`/`frame_art`), sitting just in front of the wall.
- Flag plane, sitting just *behind* the door's front face (more positive Y than the door's
  front, but still in front of the wall) — hidden by the closed door, exposed once it swings.

Getting this backwards (flag positioned in front of the door instead of behind it) is exactly
the bug that made the Q3 flag always visible regardless of the pry animation, fixed earlier
this session.

---

## Where things live

| Surface | Geoms | Materials/textures | Runtime logic |
|---|---|---|---|
| Q1 monitor password | `screen` (plane) | `mat_screen_flag` | none — static |
| Q2 flag (locked/unlocked) | `restricted_flag`, `restricted_flag_banner` (planes) | `mat_restricted_flag`, `mat_q2_painting` | `update_flag_visibility()` in `sim.py` |
| Q3 painting artwork | `frame_art` (box, on hinged `frame` body) | `mat_mona_lisa_robot` (`type="cube"`) | none — static, moves with the hinge |
| Q3 hidden flag | `painting` (plane, sibling of `frame`) | `mat_painting_flag` | none — occlusion via hinge geometry, not code |
| Q3 zone floor marker | `painting_floor` (plain rgba box, no texture) | — | `painting_solve_queue` drain in `sim.py`'s main loop |

All texture/material declarations live in `scaffolding/scene/scene.xml`'s `<asset>` block;
all geoms live in `<worldbody>` under their respective zone's body. Runtime visibility logic
(the only kind that exists today — no live texture *replacement*, just visibility toggling) is
in `scaffolding/env_api/sim.py`.
