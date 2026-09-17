# Binhu project skills

This directory is the versioned source for the Binhu-specific Codex skills. The current delivery environments are Production, Staging, and Development.

This collection intentionally does not create a `shadow-load-test` skill or change the existing Shadow runtime. Shadow cleanup and moving load tests to Staging are separate future work and are not performed by these skills.

Use `scripts/install-binhu-skills.ps1` to register these directories as Junctions under the local Codex skills directory. The script refuses to overwrite an existing same-name skill.
