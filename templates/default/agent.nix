{
  pkgs,
  tartarus,
  ...
}:

{
  imports = [
    tartarus.modules.coding
  ];

  name = "default";

  # Example shell configuration. Uncomment to customize the baseline environment
  # reachable by every jailed call. Heavier tools should stay in per-capability
  # grants so they do not bloat every shell closure.
  # shell = {
  #   packages = [ pkgs.bash pkgs.coreutils pkgs.gnugrep pkgs.findutils ];
  #   env = { GIT_PAGER = "cat"; PAGER = "cat"; };
  #   hook = ''
  #     echo "Hello Agent!"
  #   '';
  # };

  systemPrompt = ''
    You are a careful coding agent running inside Tartarus. Use only the
    tools you have been granted, and prefer the narrowest capability that
    does the job.
  '';

  model = {
    baseUrl = "https://opencode.ai/zen/v1";
    name = "glm-5.2";
    maxTokens = 32768;
    sampling = {
      temperature = 0.6;
    };
  };
}
