## Summary

Describe the user-visible problem and the focused change.

## Related issue

Link the issue or discussion when one exists.

## Safety impact

- [ ] No change to source preservation, existing-output protection, encoding chains, validation, `.partial` transfers, checksums, cancellation cleanup, paths, or credentials.
- [ ] Safety behavior changed and is explained below.

Explain any effect on original media, staging files, completed outputs, existing media-library files, tool invocation, or TMDb credentials.

## Verification

- [ ] `python -m unittest discover -s tests -v` passes.
- [ ] Relevant behavior was manually checked on Windows when needed.
- [ ] The portable build was checked when packaging or startup behavior changed.
- [ ] Documentation and changelog were updated where needed.
- [ ] No credentials, private logs, personal paths, binaries, build output, or media files are included.

List additional manual checks performed.
