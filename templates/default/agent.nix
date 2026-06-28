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
