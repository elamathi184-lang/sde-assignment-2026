import json
import os
import subprocess
import sys


def _constants_process(env_file, env_overrides=None):
    env = os.environ.copy()
    for name in (
        "MIN_VARIANT_DIALS",
        "MIN_VARIANT_CONNECTIONS",
        "MIN_INCIDENT_CALLS",
        "LOCAL_PROPOSAL_CAP",
        "EARLIEST_APPROVAL_HOUR",
        "FAMILYWISE_ERROR_RATE",
        "Z_95",
    ):
        env.pop(name, None)
    env["CONTROLLER_ENV_FILE"] = str(env_file)
    env.update(env_overrides or {})
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from autonomy_controller import constants; "
                "print(json.dumps({name: getattr(constants, name) for name in "
                "('MIN_VARIANT_DIALS', 'MIN_VARIANT_CONNECTIONS', "
                "'MIN_INCIDENT_CALLS', 'LOCAL_PROPOSAL_CAP', "
                "'EARLIEST_APPROVAL_HOUR', 'FAMILYWISE_ERROR_RATE', 'Z_95')}))"
            ),
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def test_constants_load_from_env_file_and_process_environment(tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("MIN_VARIANT_DIALS=75\nLOCAL_PROPOSAL_CAP=4\n")

    result = _constants_process(env_file, {"MIN_VARIANT_DIALS": "90"})

    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["MIN_VARIANT_DIALS"] == 90
    assert settings["LOCAL_PROPOSAL_CAP"] == 4
    assert settings["MIN_INCIDENT_CALLS"] == 40


def test_constants_reject_invalid_env_file_value(tmp_path):
    env_file = tmp_path / "invalid.env"
    env_file.write_text("MIN_VARIANT_DIALS=not-a-number\n")

    result = _constants_process(env_file)

    assert result.returncode != 0
    assert "MIN_VARIANT_DIALS must be an integer" in result.stderr


def test_constants_reject_out_of_range_policy_value(tmp_path):
    env_file = tmp_path / "invalid.env"
    env_file.write_text("EARLIEST_APPROVAL_HOUR=24\n")

    result = _constants_process(env_file)

    assert result.returncode != 0
    assert "EARLIEST_APPROVAL_HOUR must be between 0 and 23" in result.stderr
