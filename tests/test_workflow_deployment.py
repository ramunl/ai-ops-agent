"""Guard the CI dependency that protects the deployment webhook."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load_workflow(name):
    # BaseLoader preserves GitHub's `on` key (YAML 1.1 treats it as a boolean).
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def test_deployment_has_no_independent_triggers():
    workflow = load_workflow("deploy.yml")
    assert workflow["on"] == {
        "workflow_call": {"secrets": {"DEPLOY_WEBHOOK_URL": {"required": "true"}}}
    }


def test_deployment_requires_successful_checks_on_main_push():
    workflow = load_workflow("test.yml")
    assert workflow["jobs"]["deploy"] == {
        "needs": "test",
        "if": "github.event_name == 'push' && github.ref == 'refs/heads/main'",
        "uses": "./.github/workflows/deploy.yml",
        "secrets": {"DEPLOY_WEBHOOK_URL": "${{ secrets.DEPLOY_WEBHOOK_URL }}"},
    }


def test_checks_remain_blocking_and_cover_pull_requests():
    workflow = load_workflow("test.yml")
    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    }
    job = workflow["jobs"]["test"]
    assert "continue-on-error" not in job
    assert "if" not in job
    steps = job["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert steps[0]["with"] == {"submodules": "recursive"}
    commands = []
    for step in steps:
        assert "continue-on-error" not in step
        assert "if" not in step
        assert "shell" not in step  # Keep Actions' default fail-fast shell.
        commands.extend(step.get("run", "").splitlines())
    assert commands == [
        "pip install -r requirements-dev.txt",
        "ruff check ai_ops_agent tests",
        "ruff format --check ai_ops_agent tests",
        "python -m pytest -q",
    ]


def test_webhook_is_only_called_by_reusable_workflow():
    for path in WORKFLOWS.glob("*.y*ml"):
        workflow = load_workflow(path.name)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "DEPLOY_WEBHOOK_URL" in step.get("run", ""):
                    assert path.name == "deploy.yml"
