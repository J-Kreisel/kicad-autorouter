{
  description = "KiCad IPC autorouter development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonPackages = pkgs.python3Packages;
        application = pythonPackages.buildPythonApplication {
          pname = "kicad-autorouter";
          version = "0.1.9";
          format = "other";
          src = self;
          cargoDeps = pkgs.rustPlatform.importCargoLock {
            lockFile = ./Cargo.lock;
          };
          nativeBuildInputs = [
            pkgs.rustPlatform.cargoSetupHook
            pkgs.cargo
            pkgs.rustc
            pkgs.maturin
            pkgs.makeWrapper
            pythonPackages.installer
          ];
          dependencies = [ pythonPackages.kicad-python pythonPackages.pyside6 ];
          buildPhase = ''
            runHook preBuild
            maturin build --release --offline --interpreter ${pythonPackages.python.interpreter} --out dist
            runHook postBuild
          '';
          installPhase = ''
            runHook preInstall
            ${pythonPackages.python.interpreter} -m installer --prefix=$out dist/*.whl
            runHook postInstall
          '';
          postFixup = ''
            wrapProgram $out/bin/kicad-autorouter \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.kicad-small ]}
            wrapProgram $out/bin/kicad-autorouter-gui \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.kicad-small ]}
          '';
        };
        python = pkgs.python3.withPackages (ps: [ ps.kicad-python ps.pyside6 ps.pytest ]);
        launcher = pkgs.writeShellScriptBin "kicad-autorouter" ''
          exec ${python}/bin/python -m kicad_autorouter.cli "$@"
        '';
        guiLauncher = pkgs.writeShellScriptBin "kicad-autorouter-gui" ''
          exec ${python}/bin/python -m kicad_autorouter.gui "$@"
        '';
      in {
        packages.default = application;
        apps = {
          default = {
            type = "app";
            program = "${application}/bin/kicad-autorouter-gui";
          };
          gui = {
            type = "app";
            program = "${application}/bin/kicad-autorouter-gui";
          };
          cli = {
            type = "app";
            program = "${application}/bin/kicad-autorouter";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [
            launcher
            guiLauncher
            python
            pkgs.ruff
            pkgs.kicad-small
            pkgs.cargo
            pkgs.rustc
            pkgs.rustfmt
            pkgs.clippy
            pkgs.maturin
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            cargo build --release --quiet
            if [ -e "$PWD/target/release/lib_native.so" ]; then
              ln -sfn ../../target/release/lib_native.so "$PWD/src/kicad_autorouter/_native.abi3.so"
            else
              ln -sfn ../../target/release/lib_native.dylib "$PWD/src/kicad_autorouter/_native.abi3.so"
            fi
          '';
        };
      });
}
