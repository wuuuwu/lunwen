from __future__ import annotations

from importlib import resources
from pathlib import Path


def bundled_config(name: str) -> Path:
    project_locations = {
        "unscored_draft.yaml": Path("configs/rubrics/unscored_draft.yaml"),
        "three_reviewer.yaml": Path("configs/review_profiles/three_reviewer.yaml"),
        "zhejiang_undergraduate_thesis_v2.yaml": Path(
            "configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"
        ),
        "zhejiang_undergraduate_specialists_v1.yaml": Path(
            "configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml"
        ),
        "zhejiang_independent_panel_v1.yaml": Path(
            "configs/review_profiles/zhejiang_independent_panel_v1.yaml"
        ),
        "course_paper_v1.yaml": Path("configs/rubrics/course_paper_v1.yaml"),
        "course_paper_reviewers_v1.yaml": Path(
            "configs/review_profiles/course_paper_reviewers_v1.yaml"
        ),
    }
    relative = project_locations.get(name)
    if relative is None:
        raise ValueError(f"unknown bundled config: {name}")
    project_path = Path(__file__).resolve().parents[3] / relative
    if project_path.is_file():
        return project_path
    resource = resources.files("paper_reviewer.resources").joinpath("configs", name)
    return Path(str(resource))
