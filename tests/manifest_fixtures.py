from tartarus.manifest import Capability, Grant, Manifest, Param, build_manifest


def echo_manifest() -> Manifest:
    capabilities = {
        "echo": Capability(
            name="echo",
            description="Echo a message back to the caller verbatim.",
            policy="auto",
            params={
                "message": Param(
                    type="string",
                    description="The text to echo back.",
                    required=True,
                )
            },
            grants=Grant(),
            runner="echo {message}",
        )
    }

    return build_manifest(capabilities)
