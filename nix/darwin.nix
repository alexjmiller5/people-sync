# nix-darwin module: the profile scraper as a declared launchd user agent.
#
# Unlike the FDA-gated jobs this shape is usually mirrored from
# (screentime-backup, notion-finance-sync, the mini-job template), this job
# never touches TCC-protected macOS data - it only drives Chrome over its
# remote-debugging port and writes to its own state/profile dirs - so there is
# no signed .app bundle here, just a launchd agent pointed straight at the
# package's runner script. Add that machinery back only if a future revision
# needs a Full Disk Access grant.
#
# The module is a thin options-to-environment translator: all product
# behavior (launching each site's Chrome, running the scrape, logging) lives
# in bin/people-sync-agent, which ships as part of `packages.default`.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.people-sync-scrape;
  pkg = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
in
{
  options.services.people-sync-scrape = {
    enable = lib.mkEnableOption "the people-sync profile scraper";

    user = lib.mkOption {
      type = lib.types.str;
      description = "Login user the scraper runs as.";
      example = "someuser";
    };

    platforms = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "facebook" "instagram" "linkedin" ];
      description = ''
        Platforms to scrape, in order. Each gets its own Chrome profile and a
        remote-debugging port of `basePort + <index in this list>`.
      '';
    };

    dailyCaps = lib.mkOption {
      type = lib.types.attrsOf lib.types.int;
      default = { };
      description = ''
        Per-platform daily scrape-call caps, passed through as
        `PEOPLE_SYNC_DAILY_CAPS` (JSON). `people_sync.scrape.pace` ships its
        own defaults for the platforms above; this is a forward-compatible
        override hook until pace.py reads it (not yet wired - see AGENTS.md).
      '';
      example = { facebook = 150; instagram = 250; linkedin = 80; };
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}/.local/state/people-sync";
      defaultText = lib.literalExpression ''"~/.local/state/people-sync" (expanded for `user`)'';
      description = "Writable dir for scrape state (exported as PEOPLE_SYNC_STATE_DIR).";
    };

    profileDir = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}/.local/share/people-sync/sessions";
      defaultText = lib.literalExpression ''"~/.local/share/people-sync/sessions" (expanded for `user`)'';
      description = ''
        Parent dir for each platform's dedicated Chrome profile
        (`<profileDir>/<platform>`), exported as PEOPLE_SYNC_PROFILE_DIR.
      '';
    };

    basePort = lib.mkOption {
      type = lib.types.int;
      default = 9400;
      description = "Remote-debugging port for the first platform; later platforms use basePort + index.";
    };

    chromePath = lib.mkOption {
      type = lib.types.str;
      default = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
      description = "Path to the Chrome executable each platform's profile launches.";
    };

    credentialCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Command the scraper runs to obtain a platform's login credential JSON
        (exported as PEOPLE_SYNC_CREDENTIAL_COMMAND). The module never knows
        what this command is or does - it just wires it through.
      '';
    };

    emailCodeCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Command the scraper runs to fetch a 2FA code from email (exported as
        PEOPLE_SYNC_EMAIL_CODE_COMMAND).
      '';
    };

    smsCodeCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Command the scraper runs to fetch a 2FA code from SMS (exported as
        PEOPLE_SYNC_SMS_CODE_COMMAND).
      '';
    };

    schedule = lib.mkOption {
      type = lib.types.submodule {
        options = {
          hour = lib.mkOption { type = lib.types.int; default = 3; };
          minute = lib.mkOption { type = lib.types.int; default = 0; };
        };
      };
      default = { hour = 3; minute = 0; };
      description = ''
        Local time the agent fires. It runs once per firing: for each
        platform it drains that day's remaining cap (pace.py stops it there),
        so nothing further to schedule.
      '';
    };

    logDir = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.stateDir}/logs";
      defaultText = lib.literalExpression ''"''${stateDir}/logs"'';
      description = "Dir for per-platform + launchd logs (exported as PEOPLE_SYNC_LOG_DIR).";
    };
  };

  config = lib.mkIf cfg.enable {
    system.activationScripts.postActivation.text = lib.mkAfter ''
      /bin/mkdir -p ${lib.escapeShellArg cfg.stateDir} ${lib.escapeShellArg cfg.profileDir} ${lib.escapeShellArg cfg.logDir}
      /usr/sbin/chown ${lib.escapeShellArg cfg.user} ${lib.escapeShellArg cfg.stateDir} ${lib.escapeShellArg cfg.profileDir} ${lib.escapeShellArg cfg.logDir}
    '';

    launchd.user.agents.people-sync-scrape = {
      serviceConfig = {
        Label = "com.people-sync.scrape";
        ProgramArguments = [ "${pkg}/bin/people-sync-agent" ];
        EnvironmentVariables =
          {
            PEOPLE_SYNC_PLATFORMS = lib.concatStringsSep " " cfg.platforms;
            PEOPLE_SYNC_DAILY_CAPS = builtins.toJSON cfg.dailyCaps;
            PEOPLE_SYNC_STATE_DIR = cfg.stateDir;
            PEOPLE_SYNC_PROFILE_DIR = cfg.profileDir;
            PEOPLE_SYNC_BASE_PORT = toString cfg.basePort;
            PEOPLE_SYNC_CHROME_PATH = cfg.chromePath;
            PEOPLE_SYNC_LOG_DIR = cfg.logDir;
            PATH = "${pkg}/bin:/usr/bin:/bin";
          }
          // lib.optionalAttrs (cfg.credentialCommand != null) {
            PEOPLE_SYNC_CREDENTIAL_COMMAND = cfg.credentialCommand;
          }
          // lib.optionalAttrs (cfg.emailCodeCommand != null) {
            PEOPLE_SYNC_EMAIL_CODE_COMMAND = cfg.emailCodeCommand;
          }
          // lib.optionalAttrs (cfg.smsCodeCommand != null) {
            PEOPLE_SYNC_SMS_CODE_COMMAND = cfg.smsCodeCommand;
          };
        StartCalendarInterval = [ { Hour = cfg.schedule.hour; Minute = cfg.schedule.minute; } ];
        RunAtLoad = false;
        StandardOutPath = "${cfg.logDir}/launchd.log";
        StandardErrorPath = "${cfg.logDir}/launchd.err.log";
        ProcessType = "Interactive"; # drives a headed, visible Chrome
      };
    };
  };
}
