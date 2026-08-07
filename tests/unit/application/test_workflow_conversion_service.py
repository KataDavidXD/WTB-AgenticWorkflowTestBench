from types import SimpleNamespace

from wtb.application.services.project_service import WorkflowConversionService


def test_workflow_conversion_persists_sdk_project_version() -> None:
    project = SimpleNamespace(
        id="project-v3",
        name="versioned-project",
        description="version identity test",
        version=3,
        build_graph=lambda: object(),
    )

    workflow = WorkflowConversionService().convert_from_project(project)

    assert workflow.version == "3"
