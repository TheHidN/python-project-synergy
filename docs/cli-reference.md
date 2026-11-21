# CLI reference

Generated from the program's own argument parser by `scripts/gen_cli_reference.py`; do not edit by hand.
Exit codes: 0 done, 2 wrong usage, 3 refused (the message says why), 4 no registry yet (run `synergy init`).
Every command is a local program and uses no Claude model tokens.

Commands that act as an operator read that operator's own token automatically; run them in a session whose
launcher set `SYNERGY_OPERATOR`.

## Commands

| Command | What it does |
|---|---|
| `synergy init` | create the registry and the initial operators' tokens (safe to run again) |
| `synergy onboard` | onboard a new operator (registers it, writes its packet and launcher; starts nothing) |
| `synergy review` | prepare an isolated, never-prompting review folder (starts nothing) |
| `synergy request` | plain-English requests that every operator can see and take |
| `synergy inbox` | your durable notifications: new requests, assignments, results, messages |
| `synergy message` | send a durable message to one operator or to all |
| `synergy hook` | switch the optional Stop hook helper on or off (it never stops or deletes any session) |
| `synergy team` | approved team membership, scopes and holds |
| `synergy dispatch` | automatic delegation of requests and tasks to eligible operators |
| `synergy usage` | weekly Claude usage per account, used to balance new work |
| `synergy schedule` | the weights used to balance work |
| `synergy agent` | the shared pool of AI specialist agents every operator can use |
| `synergy status` | read-only report: operators, tasks, claims, workers |
| `synergy doctor` | check dependencies, configuration and the registry |
| `synergy verify` | check the event log's hash chain and print its head hash |
| `synergy rules` | the rules packet hash operators acknowledge |
| `synergy stale` | list operators not seen recently (report only, never reassigns) |
| `synergy operator` | register, acknowledge and list operators |
| `synergy task` | create, claim, assign and track tasks |
| `synergy file` | exclusive ownership of files and worktrees |
| `synergy worker` | the worker inventory |
| `synergy tmux` | the managed tmux workspace (never closes or types into anything) |

## Details

### synergy init

```
synergy init [-h] [--rules-file RULES_FILE] [--no-require-tokens]
```

| Argument | What it does |
|---|---|
| `--rules-file` | record this file's sha256 as the rules hash and write operator packets |
| `--no-require-tokens` | development only: do not require tokens for identity-sensitive actions |

### synergy onboard

```
synergy onboard [-h] [--status] [--role ROLE]
                       [--approval-ref APPROVAL_REF] [--scopes SCOPES]
                       [--admin]
                       id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--status` | report whether the operator has acknowledged its packet |
| `--role` |  |
| `--approval-ref` | the record or message showing an administrator approved this operator joining |
| `--scopes` | projects it may work on, comma separated, or * |
| `--admin` | make it an administrator too |

### synergy review

```
synergy review [-h] {prepare} ...
```

### synergy review prepare

```
synergy review prepare [-h] [--repo REPO] [--ref REF] [--model MODEL]
                              name
```

| Argument | What it does |
|---|---|
| `name` |  |
| `--repo` |  |
| `--ref` |  |
| `--model` | sonnet (default), opus, haiku or a full model id; use opus for hard or security-critical reviews |

### synergy request

```
synergy request [-h]
                       {add,list,status,plan,route,take,finish,done,suggest,handoff,adopt} ...
```

### synergy request add

```
synergy request add [-h] [--auto] [--no-auto] [--project PROJECT]
                           [--needs NEEDS]
                           text
```

| Argument | What it does |
|---|---|
| `text` |  |
| `--auto` | (default) let Synergy split and hand out the work automatically |
| `--no-auto` | record it only; do not split or assign anything |
| `--project` | the project this request belongs to |
| `--needs` | comma-separated skills the work needs, used to suggest who should take it |

### synergy request list

```
synergy request list [-h]
```

### synergy request status

```
synergy request status [-h] [id]
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy request plan

```
synergy request plan [-h] --step STEP id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--step` |  |

### synergy request route

```
synergy request route [-h] [--model MODEL] id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--model` |  |

### synergy request take

```
synergy request take [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy request finish

```
synergy request finish [-h] --evidence EVIDENCE id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--evidence` | the result, in a sentence |

### synergy request done

```
synergy request done [-h] --summary SUMMARY id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--summary` |  |

### synergy request suggest

```
synergy request suggest [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy request handoff

```
synergy request handoff [-h] --to TO id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--to` |  |

### synergy request adopt

```
synergy request adopt [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy inbox

```
synergy inbox [-h] {list,read,hook,wait} ...
```

### synergy inbox list

```
synergy inbox list [-h] [--all]
```

| Argument | What it does |
|---|---|
| `--all` |  |

### synergy inbox read

```
synergy inbox read [-h] ids [ids ...]
```

| Argument | What it does |
|---|---|
| `ids` |  |

### synergy inbox hook

```
synergy inbox hook [-h]
```

### synergy inbox wait

```
synergy inbox wait [-h] [--timeout TIMEOUT]
```

| Argument | What it does |
|---|---|
| `--timeout` |  |

### synergy message

```
synergy message [-h] {send,all} ...
```

### synergy message send

```
synergy message send [-h] [--subject SUBJECT] to text
```

| Argument | What it does |
|---|---|
| `to` |  |
| `text` |  |
| `--subject` |  |

### synergy message all

```
synergy message all [-h] text
```

| Argument | What it does |
|---|---|
| `text` |  |

### synergy hook

```
synergy hook [-h] {status,on,off} ...
```

### synergy hook status

```
synergy hook status [-h]
```

### synergy hook on

```
synergy hook on [-h]
```

### synergy hook off

```
synergy hook off [-h]
```

### synergy team

```
synergy team [-h]
                    {show,me,approve,suspend,resume,revoke,reauthorize,hold,release} ...
```

### synergy team show

```
synergy team show [-h]
```

### synergy team me

```
synergy team me [-h]
```

### synergy team approve

```
synergy team approve [-h] --approval-ref APPROVAL_REF [--scopes SCOPES]
                            [--no-assign] [--admin]
                            id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--approval-ref` |  |
| `--scopes` |  |
| `--no-assign` |  |
| `--admin` | make it an administrator (may approve, revoke, hold and reset tokens) |

### synergy team suspend

```
synergy team suspend [-h] --reason REASON id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--reason` |  |

### synergy team resume

```
synergy team resume [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy team revoke

```
synergy team revoke [-h] --reason REASON id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--reason` |  |

### synergy team reauthorize

```
synergy team reauthorize [-h] --approval-ref APPROVAL_REF
                                [--scopes SCOPES]
                                id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--approval-ref` |  |
| `--scopes` |  |

### synergy team hold

```
synergy team hold [-h] --reason REASON project
```

| Argument | What it does |
|---|---|
| `project` |  |
| `--reason` |  |

### synergy team release

```
synergy team release [-h] --reason REASON project
```

| Argument | What it does |
|---|---|
| `project` |  |
| `--reason` |  |

### synergy dispatch

```
synergy dispatch [-h] {status,pause,resume,run,nudge} ...
```

### synergy dispatch status

```
synergy dispatch status [-h]
```

### synergy dispatch pause

```
synergy dispatch pause [-h]
```

### synergy dispatch resume

```
synergy dispatch resume [-h]
```

### synergy dispatch run

```
synergy dispatch run [-h]
```

### synergy dispatch nudge

```
synergy dispatch nudge [-h] [--min-age MIN_AGE]
```

| Argument | What it does |
|---|---|
| `--min-age` | only unread items older than this many seconds |

### synergy usage

```
synergy usage [-h] {set,ingest,show} ...
```

### synergy usage set

```
synergy usage set [-h] --percent PERCENT [--resets RESETS]
                         [--reserve RESERVE]
                         account
```

| Argument | What it does |
|---|---|
| `account` |  |
| `--percent` |  |
| `--resets` | when the week resets, ISO date and time |
| `--reserve` | percent of the week to keep free |

### synergy usage ingest

```
synergy usage ingest [-h] --history HISTORY --reset-epoch RESET_EPOCH
                            account
```

| Argument | What it does |
|---|---|
| `account` |  |
| `--history` |  |
| `--reset-epoch` |  |

### synergy usage show

```
synergy usage show [-h]
```

### synergy schedule

```
synergy schedule [-h] {show,set} ...
```

### synergy schedule show

```
synergy schedule show [-h]
```

### synergy schedule set

```
synergy schedule set [-h] name value
```

| Argument | What it does |
|---|---|
| `name` |  |
| `value` |  |

### synergy agent

```
synergy agent [-h]
                     {register,list,discover,request,next,done,fail,result,cancel,handoff,requeue} ...
```

### synergy agent register

```
synergy agent register [-h] [--role ROLE] [--skills SKILLS]
                              [--model MODEL] [--window WINDOW]
                              [--approval-ref APPROVAL_REF]
                              id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--role` |  |
| `--skills` |  |
| `--model` |  |
| `--window` | its tmux window, recorded for reference only |
| `--approval-ref` | the record showing an administrator approved this specialist joining the pool |

### synergy agent list

```
synergy agent list [-h]
```

### synergy agent discover

```
synergy agent discover [-h] [--register]
```

| Argument | What it does |
|---|---|
| `--register` | register what was found (names are taken from the windows) |

### synergy agent request

```
synergy agent request [-h] [--needs NEEDS] [--agent AGENT]
                             [--priority {1,2,3}] [--model MODEL]
                             [--request REQUEST]
                             text
```

| Argument | What it does |
|---|---|
| `text` |  |
| `--needs` |  |
| `--agent` |  |
| `--priority` | 1 urgent, 2 normal, 3 low |
| `--model` |  |
| `--request` | the request this job belongs to |

### synergy agent next

```
synergy agent next [-h]
```

### synergy agent done

```
synergy agent done [-h] --result RESULT id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--result` |  |

### synergy agent fail

```
synergy agent fail [-h] --reason REASON id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--reason` |  |

### synergy agent result

```
synergy agent result [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy agent cancel

```
synergy agent cancel [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy agent handoff

```
synergy agent handoff [-h] --to TO id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--to` |  |

### synergy agent requeue

```
synergy agent requeue [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy status

```
synergy status [-h]
```

### synergy doctor

```
synergy doctor [-h]
```

### synergy verify

```
synergy verify [-h]
```

### synergy rules

```
synergy rules [-h] {set,show} ...
```

### synergy rules set

```
synergy rules set [-h] (--hash HASH | --file FILE) [--by BY]
```

| Argument | What it does |
|---|---|
| `--hash` |  |
| `--file` | a file whose sha256 becomes the rules hash |
| `--by` |  |

### synergy rules show

```
synergy rules show [-h]
```

### synergy stale

```
synergy stale [-h] [--minutes MINUTES]
```

| Argument | What it does |
|---|---|
| `--minutes` |  |

### synergy operator

```
synergy operator [-h]
                        {register,ack,heartbeat,list,session,profile,token} ...
```

### synergy operator register

```
synergy operator register [-h] [--role ROLE]
                                 [--environment ENVIRONMENT]
                                 [--account ACCOUNT]
                                 id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--role` |  |
| `--environment` |  |
| `--account` |  |

### synergy operator ack

```
synergy operator ack [-h] id hash
```

| Argument | What it does |
|---|---|
| `id` |  |
| `hash` |  |

### synergy operator heartbeat

```
synergy operator heartbeat [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy operator list

```
synergy operator list [-h]
```

### synergy operator session

```
synergy operator session [-h] --name NAME
```

| Argument | What it does |
|---|---|
| `--name` |  |

### synergy operator profile

```
synergy operator profile [-h] [--skills SKILLS] [--model MODEL]
                                [--max-tasks MAX_TASKS]
                                [--availability {AVAILABLE,BUSY,OUT_OF_USAGE}]
```

| Argument | What it does |
|---|---|
| `--skills` |  |
| `--model` |  |
| `--max-tasks` |  |
| `--availability` |  |

### synergy operator token

```
synergy operator token [-h] {rotate,backfill,reset} ...
```

### synergy operator token rotate

```
synergy operator token rotate [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy operator token backfill

```
synergy operator token backfill [-h]
```

### synergy operator token reset

```
synergy operator token reset [-h] --by BY --decision DECISION id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--by` |  |
| `--decision` | the record of who decided it |

### synergy task

```
synergy task [-h]
                    {create,start,fail,claim,assign,release,status,reassign,list} ...
```

### synergy task create

```
synergy task create [-h] --actor ACTOR [--dir DIR] [--branch BRANCH]
                           [--deliverables DELIVERABLES]
                           [--depends-on [DEPENDS_ON ...]] [--request REQUEST]
                           id description
```

| Argument | What it does |
|---|---|
| `id` |  |
| `description` |  |
| `--actor` |  |
| `--dir` |  |
| `--branch` |  |
| `--deliverables` |  |
| `--depends-on` |  |
| `--request` | the request this task belongs to |

### synergy task start

```
synergy task start [-h] id
```

| Argument | What it does |
|---|---|
| `id` |  |

### synergy task fail

```
synergy task fail [-h] --reason REASON id
```

| Argument | What it does |
|---|---|
| `id` |  |
| `--reason` |  |

### synergy task claim

```
synergy task claim [-h] id operator
```

| Argument | What it does |
|---|---|
| `id` |  |
| `operator` |  |

### synergy task assign

```
synergy task assign [-h] --by BY id operator
```

| Argument | What it does |
|---|---|
| `id` |  |
| `operator` |  |
| `--by` |  |

### synergy task release

```
synergy task release [-h] id operator
```

| Argument | What it does |
|---|---|
| `id` |  |
| `operator` |  |

### synergy task status

```
synergy task status [-h] [--evidence EVIDENCE]
                           id [OPERATOR] STATUS [[OPERATOR] STATUS ...]
```

| Argument | What it does |
|---|---|
| `id` |  |
| `rest` |  |
| `--evidence` |  |

### synergy task reassign

```
synergy task reassign [-h] --decision DECISION --by BY id new_owner
```

| Argument | What it does |
|---|---|
| `id` |  |
| `new_owner` |  |
| `--decision` |  |
| `--by` |  |

### synergy task list

```
synergy task list [-h]
```

### synergy file

```
synergy file [-h] {claim,release,list} ...
```

### synergy file claim

```
synergy file claim [-h] [--task TASK] [--kind {file,worktree}]
                          path operator
```

| Argument | What it does |
|---|---|
| `path` |  |
| `operator` |  |
| `--task` |  |
| `--kind` |  |

### synergy file release

```
synergy file release [-h] path operator
```

| Argument | What it does |
|---|---|
| `path` |  |
| `operator` |  |

### synergy file list

```
synergy file list [-h]
```

### synergy worker

```
synergy worker [-h] {register,state,list,start} ...
```

### synergy worker register

```
synergy worker register [-h] [--task TASK] [--window WINDOW]
                               [--dir DIR]
                               id operator
```

| Argument | What it does |
|---|---|
| `id` |  |
| `operator` |  |
| `--task` |  |
| `--window` |  |
| `--dir` |  |

### synergy worker state

```
synergy worker state [-h] [--by BY] [--evidence EVIDENCE] id state
```

| Argument | What it does |
|---|---|
| `id` |  |
| `state` |  |
| `--by` |  |
| `--evidence` | required for FINISHED: what shows the deliverables were checked |

### synergy worker list

```
synergy worker list [-h]
```

### synergy worker start

```
synergy worker start [-h] [--task TASK] [--dir DIR] [--allow-bypass]
                            id operator
```

| Argument | What it does |
|---|---|
| `id` |  |
| `operator` |  |
| `--task` |  |
| `--dir` |  |
| `--allow-bypass` |  |

### synergy tmux

```
synergy tmux [-h] {ensure,windows,inspect,attach-hint} ...
```

### synergy tmux ensure

```
synergy tmux ensure [-h]
```

### synergy tmux windows

```
synergy tmux windows [-h]
```

### synergy tmux inspect

```
synergy tmux inspect [-h] [--lines LINES] [--text] window
```

| Argument | What it does |
|---|---|
| `window` |  |
| `--lines` |  |
| `--text` | also print the last lines of the pane |

### synergy tmux attach-hint

```
synergy tmux attach-hint [-h]
```
