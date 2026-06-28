# Package the tartarus harness from uv.lock via uv2nix.
{
  pkgs,
  lib,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:

let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

  python =
    let
      interpreters = pyproject-nix.lib.util.filterPythonInterpreters {
        inherit (workspace) requires-python;
        inherit (pkgs) pythonInterpreters;
      };
    in
    if interpreters == [ ] then
      throw "tartarus: no Python interpreter matches requires-python = \"${workspace.requires-python}\""
    else
      builtins.elemAt interpreters 0;

  pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
    ]
  );
in
(pythonSet.mkVirtualEnv "tartarus-env" workspace.deps.default).overrideAttrs (_: {
  meta.mainProgram = "tartarus";
})
