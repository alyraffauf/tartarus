{ pkgs, agentModules }:

{
  default = {
    systemPrompt = ''
      You are a careful coding agent running inside Tartarus. Use only the
      tools you have been granted, and prefer the narrowest capability that
      does the job.
    '';

    shell = with pkgs; [ bash coreutils ];

    model = {
      baseUrl = "https://opencode.ai/zen/v1";
      name = "glm-5.2";
      maxTokens = 32768;
      sampling = {
        temperature = 0.6;
      };
    };

    capabilities = with agentModules; [
      read
      list
      write
      edit
      glob
      grep
      bash
      web_fetch
    ];
  };
}
