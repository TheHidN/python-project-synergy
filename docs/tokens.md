# Operator tokens

Every operator has its own secret token. You never copy or paste one.

- `synergy init` and `synergy operator register NAME` create a token automatically and store it in an
  owner-only file (`tokens/NAME.token` in the state folder, folder mode 700, file mode 600). The command never
  prints it. The registry stores only a hash of it.
- A session acts as one operator. Its launcher sets `SYNERGY_OPERATOR=NAME`. Commands then read that
  operator's own token file automatically. A session refuses to act as any other operator, and any
  identity-sensitive action with a missing, wrong or mismatched token is refused.
- Rotate with `synergy operator token rotate NAME` (run as that operator). It writes a new file in one step
  and the old token stops working. Existing operators that have no token yet get one with
  `synergy operator token backfill`, which then turns token checks on.

## What tokens do not do

All operators of one installation normally run as the same Linux user. A program running as that user can
read every token file and can set `SYNERGY_OPERATOR` itself. So tokens stop mistakes and confused workers
(a session acting as the wrong operator by accident). They do not isolate hostile or compromised processes
of the same user. Stronger isolation means running each operator as its own Linux user, with its own token
file readable only by that user; the file layout is already designed so that this can be added later.

## Advanced option: separate Unix users (stronger isolation)

For real isolation, give each operator its own Linux user account instead of sharing one. Then:

- each operator's token file lives in that user's own home and is readable only by that user;
- the registry folder is shared through a group that all operators belong to, with the database writable
  by that group and the token files kept out of it;
- no operator can read another operator's token, so it cannot act as another operator even deliberately.

This is not set up by the installer today because it needs administrator rights, several accounts and a shared
group, which is too much for a first install. The design keeps token files separate from the registry for exactly
this reason, so it can be added later without changing the registry. Treat tokens as protection against mistakes
until you run operators as separate users.
