{
  description = "people-sync: consolidate contact sources into the life-data people estate";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # All three runtime deps ship as nixpkgs python313Packages, so a plain
      # buildPythonApplication builds offline from the binary cache - no
      # uv2nix/pyproject-nix inputs needed for a dependency set this small.
      mkPeopleSync = pkgs:
        pkgs.python313Packages.buildPythonApplication {
          pname = "people-sync";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python313Packages.hatchling ];
          dependencies = with pkgs.python313Packages; [ httpx structlog websockets ];
          doCheck = false; # `just test` / `nix flake check` cover the suite
        };

    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPeopleSync pkgs;
      });

      checks = forAllSystems (pkgs: {
        build = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
