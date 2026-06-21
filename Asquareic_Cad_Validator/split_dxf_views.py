"""
Split a sheet-style ASCII DXF into the six standard orthographic view DXFs.

The source drawings in this project place each view inside a green
LWPOLYLINE frame.  This splitter keeps the original DXF header/tables/blocks,
replaces the ENTITIES section with the entities that fall inside each frame,
and writes files named front_view.dxf, back_view.dxf, top_view.dxf,
bottom_view.dxf, left_view.dxf, and right_view.dxf.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


VIEW_NAMES = ("front", "back", "top", "bottom", "left", "right")
LABEL_RE = re.compile(r"\b(front|back|top|bottom|left|right)\b", re.IGNORECASE)


@dataclass
class Entity:
    type: str
    pairs: list[tuple[str, str]]
    index: int


@dataclass
class Box:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    def contains_point(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (
            self.min_x - margin <= x <= self.max_x + margin
            and self.min_y - margin <= y <= self.max_y + margin
        )

    def contains_box(self, other: "Box", margin: float = 0.0) -> bool:
        return (
            self.contains_point(other.min_x, other.min_y, margin)
            and self.contains_point(other.max_x, other.max_y, margin)
        )

    def overlaps(self, other: "Box", margin: float = 0.0) -> bool:
        return not (
            self.max_x + margin < other.min_x
            or other.max_x + margin < self.min_x
            or self.max_y + margin < other.min_y
            or other.max_y + margin < self.min_y
        )


def group_values(entity: Entity, code: str) -> list[str]:
    return [value.strip() for group_code, value in entity.pairs if group_code.strip() == code]


def group_floats(entity: Entity, code: str) -> list[float]:
    values: list[float] = []
    for value in group_values(entity, code):
        try:
            values.append(float(value))
        except ValueError:
            pass
    return values


def first_value(entity: Entity, code: str, default: str = "") -> str:
    values = group_values(entity, code)
    return values[0] if values else default


def entity_points(entity: Entity) -> list[tuple[float, float]]:
    if entity.type == "LINE":
        x0, y0 = group_floats(entity, "10"), group_floats(entity, "20")
        x1, y1 = group_floats(entity, "11"), group_floats(entity, "21")
        points: list[tuple[float, float]] = []
        if x0 and y0:
            points.append((x0[0], y0[0]))
        if x1 and y1:
            points.append((x1[0], y1[0]))
        return points

    if entity.type in {"LWPOLYLINE", "SPLINE", "POLYLINE"}:
        xs = group_floats(entity, "10")
        ys = group_floats(entity, "20")
        return list(zip(xs, ys))

    if entity.type in {"CIRCLE", "ARC"}:
        xs = group_floats(entity, "10")
        ys = group_floats(entity, "20")
        radii = group_floats(entity, "40")
        if xs and ys and radii:
            x, y, radius = xs[0], ys[0], abs(radii[0])
            return [(x - radius, y - radius), (x + radius, y + radius)]

    if entity.type == "ELLIPSE":
        xs = group_floats(entity, "10")
        ys = group_floats(entity, "20")
        major_x = group_floats(entity, "11")
        major_y = group_floats(entity, "21")
        ratio = group_floats(entity, "40") or [1.0]
        if xs and ys and major_x and major_y:
            radius = ((major_x[0] ** 2 + major_y[0] ** 2) ** 0.5) * max(1.0, abs(ratio[0]))
            return [(xs[0] - radius, ys[0] - radius), (xs[0] + radius, ys[0] + radius)]

    if entity.type in {"TEXT", "MTEXT", "DIMENSION"}:
        xs = group_floats(entity, "10")
        ys = group_floats(entity, "20")
        if xs and ys:
            return [(xs[0], ys[0])]

    return []


def entity_box(entity: Entity) -> Box | None:
    points = entity_points(entity)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return Box(min(xs), min(ys), max(xs), max(ys))


def parse_ascii_dxf(path: Path) -> tuple[list[tuple[str, str]], list[Entity], list[tuple[str, str]]]:
    raw_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(raw_lines) % 2:
        raw_lines.append("")

    pairs = [(raw_lines[i], raw_lines[i + 1]) for i in range(0, len(raw_lines), 2)]

    entities_section_start: int | None = None
    entities_start: int | None = None
    entities_end: int | None = None

    for i in range(len(pairs) - 1):
        code = pairs[i][0].strip()
        value = pairs[i][1].strip()
        next_code = pairs[i + 1][0].strip()
        next_value = pairs[i + 1][1].strip()
        if code == "0" and value == "SECTION" and next_code == "2" and next_value == "ENTITIES":
            entities_section_start = i
            entities_start = i + 2
            break

    if entities_start is None or entities_section_start is None:
        raise ValueError(f"No ENTITIES section found in {path}")

    for i in range(entities_start, len(pairs)):
        if pairs[i][0].strip() == "0" and pairs[i][1].strip() == "ENDSEC":
            entities_end = i
            break

    if entities_end is None:
        raise ValueError(f"ENTITIES section is not closed in {path}")

    prefix = pairs[:entities_start]
    suffix = pairs[entities_end:]
    entities: list[Entity] = []
    current: list[tuple[str, str]] | None = None
    current_type = ""
    current_index = 0

    for pair in pairs[entities_start:entities_end]:
        code, value = pair[0].strip(), pair[1].strip()
        if code == "0":
            if current is not None:
                entities.append(Entity(current_type, current, current_index))
            current = [pair]
            current_type = value
            current_index = len(entities)
        elif current is not None:
            current.append(pair)

    if current is not None:
        entities.append(Entity(current_type, current, current_index))

    return prefix, entities, suffix


def is_green_view_frame(entity: Entity, sheet_box: Box) -> bool:
    if entity.type != "LWPOLYLINE":
        return False
    if first_value(entity, "62") != "3":
        return False
    box = entity_box(entity)
    if box is None:
        return False
    return box.area > sheet_box.area * 0.03 and box.width > 0 and box.height > 0


def clean_label(text: str) -> str:
    return re.sub(r"[^A-Za-z ]+", " ", text).strip().lower()


def assign_view_names(frames: list[tuple[Box, Entity]], entities: list[Entity]) -> dict[str, Box]:
    assignments: dict[str, Box] = {}

    for entity in entities:
        if entity.type not in {"TEXT", "MTEXT"}:
            continue
        label_text = clean_label(" ".join(group_values(entity, "1") + group_values(entity, "3")))
        match = LABEL_RE.search(label_text)
        box = entity_box(entity)
        if not match or box is None:
            continue
        x, y = box.center
        for frame, _frame_entity in frames:
            if frame.contains_point(x, y, margin=10.0):
                assignments[match.group(1).lower()] = frame
                break

    unassigned_frames = [frame for frame, _ in frames if frame not in assignments.values()]
    unassigned_names = [name for name in VIEW_NAMES if name not in assignments]

    if len(unassigned_frames) == 1 and len(unassigned_names) == 1:
        assignments[unassigned_names[0]] = unassigned_frames[0]
    elif unassigned_frames or unassigned_names:
        # Stable fallback for an unlabeled 2x3 sheet.
        ordered = sorted((frame for frame, _ in frames), key=lambda b: (-b.center[1], b.center[0]))
        fallback_order = ("front", "back", "left", "top", "bottom", "right")
        for name, frame in zip(fallback_order, ordered):
            assignments.setdefault(name, frame)

    missing = [name for name in VIEW_NAMES if name not in assignments]
    if missing:
        raise ValueError(f"Could not identify view frame(s): {', '.join(missing)}")

    return assignments


def should_include_entity(entity: Entity, frame: Box, frame_entity: Entity, margin: float) -> bool:
    if entity.index == frame_entity.index:
        return False
    if entity.type == "DIMENSION":
        return False
    if entity.type in {"TEXT", "MTEXT"}:
        return False
    if first_value(entity, "62") == "3":
        return False

    box = entity_box(entity)
    if box is None:
        return False

    # Sheet borders and title-block lines overlap every view frame. Reject
    # entities that are obviously much larger than one view before allowing
    # overlap-based inclusion, otherwise legitimate crossing edges can be lost.
    if box.width > frame.width * 1.25 or box.height > frame.height * 1.25:
        return False

    if box.area == 0.0:
        return frame.contains_point(*box.center, margin=margin)

    return frame.contains_box(box, margin=margin) or frame.overlaps(box, margin=margin)


def write_dxf(path: Path, prefix: list[tuple[str, str]], entities: list[Entity], suffix: list[tuple[str, str]]) -> None:
    lines: list[str] = []
    for code, value in prefix:
        lines.extend([code, value])
    for entity in entities:
        for code, value in entity.pairs:
            lines.extend([code, value])
    for code, value in suffix:
        lines.extend([code, value])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def split_dxf_views(source: Path, output_dir: Path, margin: float = 0.01) -> dict[str, Path]:
    prefix, entities, suffix = parse_ascii_dxf(source)
    entity_boxes = [box for entity in entities if (box := entity_box(entity)) is not None]
    if not entity_boxes:
        raise ValueError(f"No drawable entities found in {source}")

    sheet_box = Box(
        min(box.min_x for box in entity_boxes),
        min(box.min_y for box in entity_boxes),
        max(box.max_x for box in entity_boxes),
        max(box.max_y for box in entity_boxes),
    )
    frames = [(entity_box(entity), entity) for entity in entities if is_green_view_frame(entity, sheet_box)]
    frames = [(box, entity) for box, entity in frames if box is not None]
    frames.sort(key=lambda item: (-item[0].center[1], item[0].center[0]))

    if len(frames) != 6:
        raise ValueError(f"Expected 6 green view frames, found {len(frames)}")

    assignments = assign_view_names(frames, entities)
    frame_by_box = {id(box): entity for box, entity in frames}
    output_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for view_name in VIEW_NAMES:
        frame = assignments[view_name]
        frame_entity = frame_by_box[id(frame)]
        view_entities = [
            entity
            for entity in entities
            if should_include_entity(entity, frame, frame_entity, margin=margin)
        ]
        output_path = output_dir / f"{view_name}_view.dxf"
        write_dxf(output_path, prefix, view_entities, suffix)
        written[view_name] = output_path
        print(
            f"{view_name:>6}: {len(view_entities):3d} entities -> {output_path} "
            f"frame=({frame.min_x:.2f},{frame.min_y:.2f})-({frame.max_x:.2f},{frame.max_y:.2f})"
        )

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a sheet DXF into six view DXF files.")
    parser.add_argument("source", nargs="?", default="Data/drawing.dxf", type=Path)
    parser.add_argument("--output-dir", default="Data_gen", type=Path)
    parser.add_argument("--margin", default=0.01, type=float)
    args = parser.parse_args()

    split_dxf_views(args.source, args.output_dir, margin=args.margin)


if __name__ == "__main__":
    main()
