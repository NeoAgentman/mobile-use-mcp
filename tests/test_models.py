import pytest
from pydantic import ValidationError

from mobile_use_mcp.models import Bounds, PackagePolicy, Target, UIElement


def test_bounds_center_and_screen_validation() -> None:
    bounds = Bounds(x=10, y=20, width=100, height=50)

    assert bounds.center == (60, 45)
    assert bounds.is_within(1080, 2400)
    assert not Bounds(x=1080, y=20, width=10, height=10).is_within(1080, 2400)


def test_target_requires_at_least_one_selector() -> None:
    with pytest.raises(ValidationError, match="At least one"):
        Target()


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Bounds(x=1, y=2, width=3, height=4, unexpected=True)  # type: ignore[call-arg]


def test_ui_element_semantics() -> None:
    element = UIElement(text="Continue", clickable=True)

    assert element.has_semantic_content()
    assert element.is_interactive()


def test_package_policy_has_strict_allowlist_and_surface_schema() -> None:
    policy = PackagePolicy(
        allowed_packages=["com.example"],
        allowed_system_packages=["com.android.settings"],
        allowed_system_surfaces=["permission_dialog", "chooser"],
    )

    assert policy.allowed_packages == ["com.example"]
    assert policy.allowed_system_packages == ["com.android.settings"]
    assert policy.allowed_system_surfaces == ["permission_dialog", "chooser"]
    assert policy.summary().enabled is True

    with pytest.raises(ValidationError):
        PackagePolicy(allowed_packages=["not a package"])
    with pytest.raises(ValidationError):
        PackagePolicy(allowed_packages=["com..example"])
    with pytest.raises(ValidationError):
        PackagePolicy(allowed_packages=["com.example."])
    with pytest.raises(ValidationError):
        PackagePolicy(allowed_system_surfaces=["model_decision"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="Extra inputs"):
        PackagePolicy(allowed_packages=["com.example"], bypass=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="cannot both be provided"):
        PackagePolicy.model_validate(
            {"allowed_packages": ["com.example"], "allowed_app_packages": ["com.other"]}
        )


def test_package_policy_matches_only_explicit_packages_or_system_surfaces() -> None:
    policy = PackagePolicy(
        allowed_packages=["com.example"],
        allowed_system_surfaces=["permission_dialog", "chooser"],
    )

    assert policy.allows_package("com.example")
    assert policy.allows_package("COM.EXAMPLE")
    assert policy.allows_package("com.android.permissioncontroller")
    assert policy.allows_package("com.android.intentresolver")
    assert policy.allows_surface("permission")
    assert PackagePolicy(allowed_system_packages=["com.android.launcher3"]).allows_surface(
        "launcher"
    )
    assert not policy.allows_package("com.other")
    assert not policy.allows_package("com.android.settings")
    assert not policy.allows_surface("launcher")
    assert not PackagePolicy(
        allowed_system_surfaces=["permission_dialog"]
    ).allows_package("evil.permissioncontroller")
