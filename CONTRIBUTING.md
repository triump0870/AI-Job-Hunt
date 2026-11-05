# Contributing to Job Hunt OS

Thanks for helping improve Job Hunt OS. We welcome bug reports, documentation, reliability fixes, tests, integrations and thoughtful architecture proposals. This project is **source-available**, not OSI open source: commercial uses require a separate license.

## Before you contribute

1. Read [LICENSE](LICENSE) and [COMMERCIAL-LICENSING.md](COMMERCIAL-LICENSING.md). The public license allows the uses described in its terms; commercial use is **not** automatically granted.
2. Read [CLA.md](CLA.md). **Before a maintainer merges your first substantive pull request, you must sign the CLA** by commenting on your pull request (see [CLA process](#cla-process-for-contributors)). This grants **Rohan Roy personally** broad nonexclusive licensing rights, including for paid hosted services; you keep your own original copyright. If your employer owns the work, obtain its authority and any requested corporate agreement.
3. Do not include code, AI output, dataset content or documentation copied from third parties without license review. Dependence on an existing library is not the same as ownership of its code.
4. Never commit secrets, API keys, cookies, personal resumes, recruiter emails, screenshots with PII or browser profile data.

## Start a contribution

- Search existing issues and PRs first. For significant new features, open an issue discussing the problem, expected behavior, alternatives and how to test it.
- Keep pull requests narrowly scoped. One architectural phase/feature branch per PR. Explain user-facing impact, migrations, feature flag defaults and rollback.
- Follow [AGENTS.md](AGENTS.md), [docs/architecture/agentic-roadmap.md](docs/architecture/agentic-roadmap.md) and [docs/prompts.md](docs/prompts.md). New work must preserve the existing service boundaries and policies.

## Local development and checks

```bash
python -m pip install -r requirements.txt 'pytest>=8.4,<9' 'pytest-asyncio>=1.1,<2' 'ruff>=0.12,<1' 'pytest-cov>=7,<8'
python -m pytest
python -m ruff check app tests
npm ci
npx vitest run
```

Run both suites: `pytest` for the backend and `npx vitest run` for `app/static/*.js`. Ranking and cleanup tests need
PostgreSQL (`TEST_DATABASE_URL`) and are skipped on SQLite. Provide commands and results actually run; never claim full-suite success if environment-dependent tests were skipped. Use disposable databases for migration checks and avoid production API credentials in tests.

## Project-specific engineering rules

- All LLM calls use the existing centralized LLM gateway and configurable worker model routing.
- All external provider calls go through the instrumentation layer.
- No direct cross-user database access; use tenant-aware APIs and retrieval constraints.
- Keep verified candidate evidence and approved resumes immutable at the applicable approval boundary.
- Preserve the existing MCP/application queue. Every submission takes a pre-submit claim (`apply_queue.begin_submission`); only the guarded path may click Submit, and only for an account that switched auto submit on; everything else is submitted by a person. No CAPTCHA bypass or misleading applied state.
- Gmail read access is scoped, and collected third-party content is untrusted.
- Never add uninstrumented scraping, stealth bypass, credential capture or undisclosed telemetry.

## PR checklist

- [ ] Relevant issue or summary provided; scope is clear.
- [ ] Tests cover changes and the results are documented.
- [ ] Security, privacy and tenant isolation were evaluated.
- [ ] New API, migration, model routing, and prompt behavior is documented.
- [ ] Third-party materials and attribution are disclosed.
- [ ] No keys, secrets or private candidate data are included.
- [ ] I have signed the CLA on this pull request, and the `CLA Assistant` check is green.

## CLA process for contributors

The CLA is signed on the pull request itself, once per GitHub account, through the project's CLA Assistant workflow
([CLA Assistant Lite](https://github.com/contributor-assistant/github-action)):

1. Open your pull request. The CLA Assistant bot comments on it, links to [CLA.md](CLA.md) and sets the
   `CLA Assistant` check.
2. Read the agreement, then sign it by posting this comment on the pull request, exactly as written:

   ```
   I have read the Job Hunt OS CLA version 1.0 and I hereby sign it
   ```

   The bot records your GitHub account, the comment and the time in the project's signature record
   (`signatures/version1/cla.json` on the `cla-signatures` branch). The signature covers this and all your future pull
   requests.
3. The check turns green. A pull request is merged only with a green `CLA Assistant` check. If it stays red after you
   signed, comment `recheck`.

- **Only that exact comment is a signature.** A checkbox, a differently worded comment or a `Signed-off-by` line is
  not.
- **Every commit author must sign.** The bot checks the GitHub account of each commit's author. If a commit's email is
  not on your GitHub account, the bot cannot match it: add that email to your account (GitHub → Settings → Emails) or
  rewrite the commit with your account's email.
- **Work owned by an employer** also needs that employer's written permission (CLA Section 6). Say so in the pull
  request, and a maintainer will arrange it privately before merging. Do not post identity documents or your home
  address in the pull request.
- **A new CLA version** asks you to accept it again before your next pull request is merged; rights you already granted
  stay in force.
- Contributors retain their original copyright and may reuse their original code elsewhere, subject to others’ rights, while granting Rohan Roy broad commercial sublicensing/relicensing rights. Merely contributing does not create a royalty/equity entitlement.

## Code review and maintenance

Maintainers may request changes or decline a contribution for technical, legal, or security reasons. Submitting a PR does not create a right to merge, compensation, an equity interest or ownership of Job Hunt OS trademarks. Rohan Roy (and lawful successors or authorized sublicensees) may adapt and commercially license Contributions within the rights granted by each signed CLA. Contributors retain rights in their own original work as set out in that agreement.

## Security vulnerabilities

Please do **not** disclose an exploitable vulnerability publicly before coordinated disclosure. Use the repository’s GitHub private vulnerability reporting feature (see [SECURITY.md](SECURITY.md)). If the private reporting button is unavailable, do not publish exploit details in an issue; wait for a maintainer to enable it or provide a verified private channel.

## Conduct

Be respectful and constructive, and assume good faith. Harassment, discrimination, exposing personal information, spam and aggressive conduct are not tolerated. Maintainers may moderate participation to protect the community.
