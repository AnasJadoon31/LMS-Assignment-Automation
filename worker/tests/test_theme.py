import json

import pytest

from lms_worker.theme import LOGO, THEME_DIR, Student, import_artifact, install

STUDENT = Student(name="Ali Raza", enrollment="02-111111-001", klass="BS(CS) 3-A")


def artifact(tmp_path):
    """A downloaded copy of the theme artifact's files, as the artifact names them."""
    p = tmp_path / "download" / "project"
    (p / "components/ReportCover").mkdir(parents=True)
    (p / "assets/Logos").mkdir(parents=True)
    (p / "README.md").write_text("# Theme\n\n| Name | Ali Raza |\n| Enrollment | 02-111111-001 |\n| Class | BS(CS) 3-A |\n"
                                 "The semester comes from the class (3-A is Semester 03). Notebook: AI_Lab_05_Ali_Raza.\n")
    (p / "tokens.json").write_text(json.dumps({"color": {"tokens": [{"name": "navy", "value": "#2d398c"}]}}))
    (p / "design-system.json").write_text('{"v": 3, "lastChange": {"by": "Ali Raza"}}')
    (p / "components/ReportCover/preview.html").write_text('<img src="/_blob/abc123"><td>Ali Raza</td><td>02-111111-001</td>')
    (p / LOGO).write_bytes(b"\x89PNG fake")
    return p.parent


def test_mirror_keeps_the_artifact_and_hides_only_the_student(tmp_path):
    dest = tmp_path / "mirror"
    written = import_artifact(artifact(tmp_path), STUDENT, version="v7", logo_blob="abc123", dest=dest)
    assert sorted(written) == ["README.md", "assets/Logos/bahria-university.png", "components/ReportCover/preview.html", "tokens.json"]
    text = "".join((dest / f).read_text() for f in ("README.md", "components/ReportCover/preview.html"))
    for value in ("Ali Raza", "02-111111-001", "BS(CS) 3-A", "3-A"):
        assert value not in text
    assert "AI_Lab_05_{{STUDENT_NAME_UNDERSCORED}}" in text and "({{SECTION}} is Semester 03)" in text
    assert 'src="../../assets/Logos/bahria-university.png"' in text
    assert not (dest / "design-system.json").exists()  # the artifact's index, not theme content
    assert json.loads((dest / "MIRROR.json").read_text())["version"] == "v7"


def test_installed_theme_is_the_artifact_again(tmp_path):
    src = artifact(tmp_path)
    mirror = tmp_path / "mirror"
    import_artifact(src, STUDENT, logo_blob="abc123", dest=mirror)
    out = install(tmp_path / "work" / "theme", STUDENT, src=mirror)
    assert (out / "README.md").read_text() == (src / "project/README.md").read_text()
    assert (out / "tokens.json").read_text() == (src / "project/tokens.json").read_text()
    assert (out / LOGO).read_bytes() == b"\x89PNG fake"
    assert not (out / "MIRROR.json").exists()


def test_a_download_without_the_logo_is_refused(tmp_path):
    src = artifact(tmp_path)
    (src / "project" / LOGO).unlink()
    with pytest.raises(FileNotFoundError, match="logo"):
        import_artifact(src, STUDENT, dest=tmp_path / "mirror")


def test_the_shipped_mirror_fills_in(tmp_path):
    readme = (THEME_DIR / "README.md").read_text()
    assert "{{STUDENT_NAME}}" in readme and "{{ENROLLMENT}}" in readme and "{{CLASS}}" in readme
    out = install(tmp_path / "theme", STUDENT)
    filled = (out / "README.md").read_text()
    assert "{{" not in filled and "| Name | Ali Raza |" in filled and "| Enrollment | 02-111111-001 |" in filled
    assert (out / LOGO).exists() and (out / "components/ReportCover/preview.html").exists()
