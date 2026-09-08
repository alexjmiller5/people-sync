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
# behavior (attaching to or launching Chrome, running the scrape, logging)
# lives in bin/people-sync-agent, which ships as part of `packages.default`.
#
# Two ways to reach a browser: `endpoint` attaches every platform to one
# already-running Chrome (a shared profile whose logins persist for every
# job on the machine - the recommended shape), or, with `endpoint` unset,
# each platform gets its own headed Chrome profile and debug port.
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
        Platforms to scrape, in order. With `endpoint` unset each gets its
        own Chrome profile and a remote-debugging port of
        `basePort + <index in this list>`.
      '';
    };

    endpoint = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "127.0.0.1:9222";
      description = ''
        `host:port` of a Chrome that is already listening for remote
        debugging. When set, every platform attaches to it (one shared
        profile, its sessions reused by every job and every manual login on
        the machine) and no Chrome is launched - `profileDir`, `basePort` and
        `chromePath` are unused. Exported as PEOPLE_SYNC_ENDPOINT.
      '';
    };

    dailyCaps = lib.mkOption {
      type = lib.types.attrsOf lib.types.int;
      default = { };
      description = ''
        Per-platform daily scrape-call caps, exported as `PEOPLE_SYNC_DAILY_CAPS`
        (a JSON object) and merged over the scraper's built-in defaults when it
        starts; platforms not listed keep their default.
      '';
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
        Command that prints a platform's login credential as JSON
        (`{"username": ..., "password": ..., "totp": ...}`, `totp` being the
        current one-time code or null). Exported as
        PEOPLE_SYNC_CREDENTIAL_COMMAND and run by `login` as
        `sh -c "<command>" people-sync-login <platform>` (the platform is `$1`)
        with a 60 s timeout; a non-zero exit halts the login. The module
        never knows what the command does.
      '';
    };

    emailCodeCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Command that prints the newest one-time code received by email, or
        nothing when none has arrived yet (`login` polls it every 5-10 s for
        up to 90 s). Same invocation and timeout as credentialCommand;
        exported as PEOPLE_SYNC_EMAIL_CODE_COMMAND.
      '';
    };

    smsCodeCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Command that prints the newest one-time code received by SMS on this
        machine, or nothing when none has arrived yet. Same invocation,
        polling and timeout as emailCodeCommand; exported as
        PEOPLE_SYNC_SMS_CODE_COMMAND.
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
          // lib.optionalAttrs (cfg.endpoint != null) {
            PEOPLE_SYNC_ENDPOINT = cfg.endpoint;
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
