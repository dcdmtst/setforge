"""Project bootstrapping and immutable input snapshots."""

import shutil
from importlib.resources import files
from pathlib import Path

from .io import inside, read, sha, write
from .schemas import ENTITY, validate
from .schemas import contracts


def init_project(project, materials=None):
    project = Path(project).resolve()
    if project.exists() and any(project.iterdir()):
        raise ValueError("Project folder must be empty; existing files will not be overwritten.")
    project.mkdir(parents=True, exist_ok=True)
    defaults = files("setforge") / "defaults"
    for name in ("project.json", "style.md"):
        (project / name).write_text((defaults / name).read_text(encoding="utf-8"), encoding="utf-8")
    refs = []
    if materials:
        from .stylepack import import_pack
        refs,style,collection_id=import_pack(materials,project)
        (project/'style.md').write_text(style,encoding='utf8')
        cfg=read(project/'project.json');cfg['global_reference_ids']=[r['id'] for r in refs[:3]]
        cfg['collection_id']=collection_id
        write(project/'project.json',cfg)
    write(project / "references.json", refs)
    write(project / "canon.json", [])
    return project


def check_project(project):
    project = Path(project).resolve()
    cfg = read(project / "project.json")
    contracts(cfg.get("schema_version", 1))
    refs = read(project / "references.json")
    if not refs:
        raise ValueError("Reference catalogue is empty. Import reference images before running.")
    seen = set()
    for ref in refs:
        if ref["id"] in seen:
            raise ValueError(f"Duplicate reference ID: {ref['id']}")
        seen.add(ref["id"])
        source = inside(project, ref["path"])
        if sha(source) != ref["sha256"]:
            raise ValueError(f"Reference changed without updating its hash: {ref['id']}")
    if not set(cfg["global_reference_ids"]).issubset(seen):
        raise ValueError("Unknown global reference ID.")
    if cfg["rules"]["cards_per_variant"] != 10:
        raise ValueError("This pipeline supports exactly 10 cards per variant.")
    if type(cfg["rules"].get("select_before_art", False)) is not bool:
        raise ValueError("select_before_art must be boolean.")
    if type(cfg['rules'].get('exclude_main_cast_scenes', False)) is not bool:
        raise ValueError('exclude_main_cast_scenes must be boolean.')
    if type(cfg['rules'].get('exclude_golden_cards', False)) is not bool:
        raise ValueError('exclude_golden_cards must be boolean.')
    if type(cfg['rules'].get('title_preflight',True)) is not bool:
        raise ValueError('title_preflight must be boolean.')
    if cfg['rules'].get('title_preflight',True) is False:
        raise ValueError('Title preflight is mandatory for new runs; it cannot be disabled.')
    if cfg['rules'].get('characters', 'optional') not in ('none', 'optional', 'required'):
        raise ValueError('characters must be none, optional or required.')
    if set(cfg["rules"]["background_categories"]) != {1, 2, 3, 4}:
        raise ValueError("Required background categories must be 1,2,3,4.")
    size = cfg["rules"]["output_size"]
    if len(size) != 2 or any(type(n) is not int or n < 64 or n > 8192 for n in size):
        raise ValueError("Invalid output_size.")
    limits = cfg["limits"]
    for field in ("max_calls", "max_attempts_per_task", "max_set_revisions", "timeout_seconds"):
        if type(limits[field]) is not int or limits[field] < 1:
            raise ValueError(f"Invalid limit: {field}")
    if not isinstance(limits["max_estimated_cost"], (int, float)) or limits["max_estimated_cost"] < 0:
        raise ValueError("Invalid max_estimated_cost.")
    for role in ("planner", "reviewer", "image"):
        provider = cfg["providers"][role]
        if provider["type"] not in {"files", "command", "codex", "chat-completions", "image-cli"}:
            raise ValueError(f"Unknown provider type for {role}.")
        if role == "image" and provider["type"] in {"codex", "chat-completions"}:
            raise ValueError("Text provider cannot generate image artifacts.")
        if role != "image" and provider["type"] == "image-cli":
            raise ValueError("image-cli is only an image provider.")
        if any(key in provider for key in ("api_key", "token", "password")):
            raise ValueError("Put credentials in environment variables, not project.json.")
        if provider.get("estimated_cost", -1) < 0:
            raise ValueError(f"Set non-negative estimated_cost for {role}.")
        if cfg.get("schema_version", 1) == 2 and limits["max_estimated_cost"] == 0:
            if provider["type"] != "files" and not provider.get("zero_cost", False):
                raise ValueError("Zero budget requires an explicitly free/local provider: " + role)
            if provider["estimated_cost"] != 0:
                raise ValueError("Zero budget forbids positive per-call estimates")
        if provider["type"] == "command" and (not isinstance(provider.get("argv"), list)
                                               or not provider["argv"]):
            raise ValueError("Command provider needs an argv list.")
    return cfg, refs


def snapshot(project, run):
    cfg, refs = check_project(project)
    project, run = Path(project), Path(run)
    for ref in refs:
        dest = run / "inputs" / ref["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inside(project, ref["path"]), dest)
    canon = read(project / "canon.json")
    keys = set()
    for entity in canon:
        validate({key: entity[key] for key in ENTITY["properties"]}, ENTITY)
        key = entity["id"]
        if key in keys:
            raise ValueError("canon.json must contain one active version per entity ID.")
        keys.add(key)
        if entity.get("scope") != "collection":
            raise ValueError("Project canon entries must have collection scope.")
        sources = entity["images"]
        if not sources:
            raise ValueError(f"Canon entity {key} has no images.")
        for index, source in enumerate(sources):
            original = inside(project, source)
            dest = Path("inputs/canon") / f"{key}-v{entity['version']}-{index}{original.suffix}"
            target = inside(run, dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            sources[index] = dest.as_posix()
    write(run / "inputs" / "project.json", cfg)
    write(run / "inputs" / "references.json", refs)
    write(run / "inputs" / "canon.json", canon)
    shutil.copy2(project / "style.md", run / "inputs" / "style.md")
    if (project / "research").exists():
        shutil.copytree(project / "research", run / "inputs" / "research")
    manifest = [{"path": p.relative_to(run).as_posix(), "sha256": sha(p)}
                for p in sorted((run / "inputs").rglob("*")) if p.is_file()]
    write(run / "inputs" / "manifest.json", manifest)
    return cfg, canon
