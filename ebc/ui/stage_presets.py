"""Stage presets and identity as the UI shows them: the Stages list rows, their collections, the enum
items of the stage pickers. No state except the enum items cache, and no import of the other UI
modules: ``props`` (the enum callbacks, Show only the matching stage collection) and ``stages``
(operators, Load stage) both import this one.
"""

from .. import runtime
from ..core import stages as st

# -- identity --------------------------------------------------------------------------------


def stage_label(s) -> str:
    """'Battlefield (L) — slot 01 · BATTLEFIELD_L_BF_MELEE', or a dash."""
    if not s.stage_key:
        return "—"
    parsed = st.split_key(s.stage_key)
    stem = parsed[1] if parsed else s.stage_key
    return f"{s.stage_name} — slot {s.stage_slot:02X} · {stem}"


def set_identity(s, stage: st.StageIdentity | None) -> None:
    name, slot, key = (stage.name, stage.slot, stage.key) if stage else ("", -1, "")
    if s.stage_name != name:
        s.stage_name = name
    if s.stage_slot != slot:
        s.stage_slot = slot
    if s.stage_key != key:
        s.stage_key = key


# -- presets ---------------------------------------------------------------------------------


def refs(s) -> list[st.PresetRef]:
    return [st.PresetRef(e.key, e.match_variants, e.enabled) for e in s.stage_presets]


def matching_preset(s, key: str | None):
    index = st.select_preset(refs(s), key)
    return None if index is None else s.stage_presets[index]


def _overrides(group) -> st.StageOverrides:
    """The overridable values of a Stages row, or of the settings (same property names)."""
    return st.StageOverrides(group.near_object, group.far_object, group.pause_frame)


def current_row(s):
    """The row in force for the stage being played: None when presets are off or no row matches."""
    return matching_preset(s, s.stage_key) if s.stage_presets_enabled and s.stage_key else None


def effective_overrides(s, row=None) -> st.StageOverrides:
    """Near / Far objects and Pause at frame in force now: the current stage's row where it sets
    them, else the global ones (``row``: the current row, when the caller already has it)."""
    row = current_row(s) if row is None else row
    return st.effective_overrides(None if row is None else _overrides(row), _overrides(s))


def find_layer_collection(layer, coll):
    if layer.collection == coll:
        return layer
    for child in layer.children:
        found = find_layer_collection(child, coll)
        if found is not None:
            return found
    return None


def apply_collections(s, view_layer, key: str | None) -> int:
    """Show the matching row's collection, hide the other rows' collections (viewport of this view
    layer + render). ``key`` None shows every listed collection again. Returns how many changed."""
    listed = []
    for e in s.stage_presets:
        if e.collection is not None and e.collection not in listed:
            listed.append(e.collection)
    if key is None:
        keep = set(listed)
    else:
        row = matching_preset(s, key)
        if row is None:
            return 0  # no row for this stage: nothing touched
        keep = {row.collection} if row.collection is not None else set()
    changed = 0
    for coll in listed:
        hide = coll not in keep
        layer = find_layer_collection(view_layer.layer_collection, coll) if view_layer is not None else None
        if layer is not None and layer.hide_viewport != hide:
            layer.hide_viewport = hide
            changed += 1
        if coll.hide_render != hide:
            coll.hide_render = hide
            changed += 1
    return changed


def preset_label(stage: st.StageIdentity | None, key: str) -> str:
    if stage is not None:
        return stage.name
    parsed = st.split_key(key)
    return key if parsed is None else f"{parsed[1]}"


def ensure_preset(s, key: str, name: str):
    """The row of ``key`` (case-insensitive), added with ``name`` when there is none; it becomes the
    selected row."""
    index = next((i for i, e in enumerate(s.stage_presets) if e.key.upper() == key.upper()), None)
    if index is None:
        row = s.stage_presets.add()
        row.key = key
        row.name = name
        index = len(s.stage_presets) - 1
    s.stage_presets_index = index
    return s.stage_presets[index]


# -- enum items ------------------------------------------------------------------------------

_items_cache: dict[tuple, list] = {}  # enum items must stay alive while Blender shows them


def profile_name() -> str:
    return runtime.get_game().p.name


def slot_items(_self=None, _context=None) -> list:
    """'Stage to load': one item per playable slot, by ID: '2E' -> '2E  Pokemon Stadium 2'."""
    name = profile_name()
    items = _items_cache.get((name,))
    if items is None:
        table = st.table_for(name)
        items = [
            (f"{slot:02X}", f"{slot:02X}  {table[slot][0]}", f"Project+ stage slot {slot:02X} ({slot})")
            for slot in sorted(table)
            if slot not in st.NOT_PLAYABLE
        ]
        _items_cache[(name,)] = items
    return items


def variant_items(slot_ident: str) -> list:
    """The ASL variants of a slot: base first ('0000'), then L / R / Z and the alternates."""
    name = profile_name()
    key = (name, slot_ident)
    items = _items_cache.get(key)
    if items is None:
        items = []
        try:
            slot = int(slot_ident, 16)
        except (TypeError, ValueError):
            slot = -1
        seen = set()
        for v in st.table_for(name).get(slot, ("", ()))[1]:
            if v.pac.upper() in seen:
                continue
            seen.add(v.pac.upper())
            label = "Base" if not v.label else (f"{v.label} button" if len(v.label) == 1 else v.label)
            items.append((f"{v.button:04X}", label, f"{v.pac} (ASL 0x{v.button:04X})"))
        if not items:
            items = [("0000", "Base", "Base stage")]
        _items_cache[key] = items
    return items


def stage_items(_self=None, _context=None) -> list:
    """Every playable stage and variant 'SS:BBBB' -> 'Battlefield (L)' (Add stage)."""
    name = profile_name()
    items = _items_cache.get((name, "all"))
    if items is None:
        items = [
            (f"{v.slot:02X}:{v.button:04X}", f"{v.slot:02X}  {v.name}", f"slot {v.slot:02X} · {v.pac}")
            for v in sorted(st.reload_choices(name), key=lambda v: (v.slot, v.button))
        ]
        _items_cache[(name, "all")] = items
    return items


def parse_item(ident: str) -> tuple[int, int]:
    slot, button = ident.split(":")
    return int(slot, 16), int(button, 16)
