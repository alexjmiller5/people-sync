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
          doCheck = false; # the suite runs through `just test`; `nix flake check` only builds the package
          # The agent runbook ships with the CLI; the home module links it into
          # the agent's skill catalog.
          postInstall = ''
            mkdir -p $out/share/people-sync/skills
            cp -r ${./skills}/people-sync $out/share/people-sync/skills/people-sync
          '';
        };

    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPeopleSync pkgs;
      });

      checks = forAllSystems (pkgs: {
        build = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      });

      # `programs.people-sync`: install the CLI wrapped with the facts only the
      # operator knows (which Chrome to attach to, which commands yield
      # credentials, which Notion data sources are theirs) and put the runbook
      # skill where an agent finds it. No secret value is evaluated by Nix.
      homeModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.programs.people-sync;
          env = lib.filterAttrs (_: v: v != null) {
            PEOPLE_SYNC_CDP_ENDPOINT = cfg.endpoint;
            PEOPLE_SYNC_CDP_APPROVE_COMMAND = cfg.approveCommand;
            PEOPLE_SYNC_CREDENTIAL_COMMAND = cfg.credentialCommand;
            PEOPLE_SYNC_EMAIL_CODE_COMMAND = cfg.emailCodeCommand;
            PEOPLE_SYNC_SMS_CODE_COMMAND = cfg.smsCodeCommand;
            PEOPLE_SYNC_NOTION_PEOPLE_DS = cfg.notion.peopleDataSource;
            PEOPLE_SYNC_NOTION_RELATIONS =
              if cfg.notion.relations == { } then null else builtins.toJSON cfg.notion.relations;
          };
          wrapped = pkgs.symlinkJoin {
            name = "people-sync-configured";
            paths = [ cfg.package ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              wrapProgram $out/bin/people-sync ${
                lib.concatStringsSep " " (lib.mapAttrsToList
                  (name: value: "--set-default ${name} ${lib.escapeShellArg value}") env)
              }
            '';
          };
        in
        {
          options.programs.people-sync = {
            enable = lib.mkEnableOption "the people-sync CLI and its agent runbook";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              description = "The people-sync package to install.";
            };
            endpoint = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              example = "127.0.0.1:9222";
              description = "host:port of the Chrome DevTools endpoint the browser commands attach to.";
            };
            approveCommand = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Command that answers the browser's remote-debugging prompt on hosts that show one.";
            };
            credentialCommand = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Command printing {username, password, totp} for `login <platform>` ($1 = platform). Unset: login only verifies an existing session.";
            };
            emailCodeCommand = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Command printing the newest emailed one-time code after $PEOPLE_SYNC_CODE_AFTER.";
            };
            smsCodeCommand = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Command printing the newest SMS one-time code after $PEOPLE_SYNC_CODE_AFTER.";
            };
            notion.peopleDataSource = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Notion data-source id of the People database `new-person` creates stub pages in.";
            };
            notion.relations = lib.mkOption {
              type = lib.types.attrsOf (lib.types.listOf lib.types.str);
              default = { };
              example = { Gifts = [ "<data_source_id>" "<relation property id>" ]; };
              description = "Notion databases relating to People, checked by `reconcile merge`: label -> [data_source_id, relation property id].";
            };
            skill.enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Expose the runbook skill at $XDG_DATA_HOME/people-sync/skills/people-sync for linking into an agent's skill catalog.";
            };
          };

          config = lib.mkIf cfg.enable {
            home.packages = [ wrapped ];
            xdg.dataFile."people-sync/skills/people-sync" = lib.mkIf cfg.skill.enable {
              source = "${cfg.package}/share/people-sync/skills/people-sync";
              recursive = true;
            };
          };
        };

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
