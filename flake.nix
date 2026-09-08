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

          # bin/people-sync-agent: the launchd runner script, part of the
          # package so the nix-darwin module stays a thin options-to-env
          # translator (see nix/darwin.nix). @people_sync_bin@ is resolved
          # to this same package's `people-sync` entry point.
          postInstall = ''
            install -Dm755 ${./scripts/people-sync-agent} $out/bin/people-sync-agent
            substituteInPlace $out/bin/people-sync-agent \
              --replace-fail '@people_sync_bin@' "$out/bin/people-sync"
          '';
        };

      # Eval-only smoke test for the darwin module: runs it through the
      # module system with a minimal stand-in for the real nix-darwin option
      # tree (just the two option paths the module writes to) and forces
      # evaluation of the result. Catches option-shape mistakes without
      # depending on a nix-darwin flake input.
      darwinModuleEvalCheck = pkgs:
        let
          fixture = { lib, ... }: {
            options = {
              launchd.user.agents = lib.mkOption {
                type = lib.types.attrsOf (lib.types.attrsOf lib.types.anything);
                default = { };
              };
              system.activationScripts.postActivation.text = lib.mkOption {
                type = lib.types.lines;
                default = "";
              };
            };
          };
          evaluated = lib.evalModules {
            modules = [
              fixture
              self.darwinModules.default
              { services.people-sync-scrape = { enable = true; user = "test"; }; }
            ];
            specialArgs = { inherit pkgs; };
          };
          forced = builtins.toJSON {
            agents = builtins.attrNames evaluated.config.launchd.user.agents;
            env = evaluated.config.launchd.user.agents.people-sync-scrape.serviceConfig.EnvironmentVariables;
            hasActivation = evaluated.config.system.activationScripts.postActivation.text != "";
          };
        in
        pkgs.runCommand "people-sync-darwin-module-eval" { } ''
          cat > "$out" <<'EOF'
          ${forced}
          EOF
        '';
    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPeopleSync pkgs;
      });

      darwinModules.default = import ./nix/darwin.nix self;

      checks = forAllSystems (pkgs: {
        build = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        darwin-module-eval = darwinModuleEvalCheck pkgs;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
