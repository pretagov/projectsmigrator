"""Projects Migrator: Sync Zenhub workspaces into a single Github Project

Usage:
  projectsmigrator PROJECT_URL [--workspace=NAME]... [--exclude=FIELD:PATTERN]... [--field=SRC:DST]... [options]
  projectsmigrator (-h | --help)

Options:
  -w=NAME, --workspace=NAME            Name of a Zenhub workspace to import or none means include all.
  -f=SRC:DST:CNV, --field=SRC:DST:CNV  Transfer SRC field to DST field. "Text" as DST will add a checklist
                                       for Epic and Blocking issues, and values into the text for other fields.
                                       CNV "Scale" (match by rank), Exact or Closest (default).
                                       One SRC can have many DST fields.
                                       [Default: Estimate:Size:Scale, Priority:Priority, Pipeline:Status,
                                       Linked Issues:Text, Epic:Text, Blocking:Text, Sprint:Iteration]
                                       "SRC:" Will not transfer this field
  -x=FIELD:PAT, --exclude=FIELD:PAT    Don't include issues with field values that match the pattern
                                       e.g. "Workspace:Private*", "Pipeline:Done".
  --disable-remove                     Project items not found in any of the workspace won't be removed.
  --github-token=<token>               or use env var GITHUB_TOKEN.
  --zenhub-token=<token>               or use env var ZENHUB_TOKEN.
  --timeout=<seconds>                  How long to wait for apis [Default: 180]
  -h, --help                           Show this screen.

For ZenHub the following fields are available.
- Estimate, Priority, Pipeline, PR, Epic, Blocking, Sprint, Position, Workspace

For Projects the fields are customisable. However the following are special
- Status: the column on the board
- Position: Id of the item to place after
- Text: turns the value into a checklist/list in the body
- Linked Pull Requests: changes to the body of each PR to link back to the Issue  

"""

from docopt import docopt
import difflib
from gql import gql, Client, transport
from gql.transport.aiohttp import AIOHTTPTransport
import os
import fnmatch

default_mapping = [
    "Estimate:Size:Scale",
    "Priority:Priority",
    "Pipeline:Status",
    "Linked Issues:Text",
    "Epic:Text",
    "Blocked By:Text",
    "Sprint:Iteration",
    "Position:Position",
]

# Special field to signify putting into body text
TEXT = {"type": "body"}
FIXES = {"type": "Linked pull requests"}


def merge_workspaces(project_url, workspace, field, **args):
    # Create a GraphQL client using the defined transport
    token = os.environ["ZENHUB_TOKEN"] if not args["zenhub_token"] else args["zenhub_token"]
    zh_query = Client(
        transport=AIOHTTPTransport(
            url="https://api.zenhub.com/public/graphql",
            headers={"Authorization": f"Bearer {token}"},
        ),
        fetch_schema_from_transport=True,
        serialize_variables=True,
        execute_timeout=int(args['timeout']),
    ).execute
    token = os.environ["GITHUB_TOKEN"] if not args["github_token"] else args["github_token"]
    gh_query = Client(
        transport=AIOHTTPTransport(
            url="https://api.github.com/graphql",
            headers={"Authorization": f"Bearer {token}"},
        ),
        fetch_schema_from_transport=False,
        serialize_variables=True,
        execute_timeout=int(args['timeout']),
    ).execute

    org_name = project_url.split("orgs/")[1].split("/")[0]
    proj_num = project_url.split("projects/")[1].split("/")[0]

    # owner = gh_query(gh_user)['viewer']
    org = gh_query(gh_org, dict(login=org_name, number=int(proj_num)))
    proj = org["organization"]["projectV2"]
    all_fields = {
        f["name"]: f
        for f in gh_query(gh_get_Fields, dict(proj=proj["id"]))["node"]["fields"]["nodes"]
    }
    all_fields['Position'] = dict(name="Position")

    # Get proj states

    # Get all the items so we can speed up queries and reduce updates. But don't get body yet due to rate limiting.
    items = []
    cursor = None
    print("Reading Project", end="")
    while True:
        res = gh_query(gh_proj_items, dict(login=org_name, number=int(proj_num), cursor=cursor))[
            "organization"
        ]["projectV2"]["items"]
        items.extend(res["nodes"])
        print(".", end="")
        if not res["pageInfo"]["hasNextPage"]:
            break
        cursor = res["pageInfo"]["endCursor"]
    print()

    # Record order/after so we can see if it needs moving
    board = {}
    for i in items:
        status = field_value(i, all_fields['Status'])
        cache_init_board(i, board=board)
        cache_after_new(i, status)
    items = {issue_key(item["content"]): item for item in items}

    # map excludes
    exclude = {}
    for f, pat in (m.split(":") for m in args["exclude"]):
        exclude.setdefault(f, []).append(pat)
    del args["exclude"]

    workspaces = {
        ws["name"]: ws for ws in zh_query(zh_workspaces)["recentlyViewedWorkspaces"]["nodes"]
    }
    if not workspace:
        workspace = list(workspaces.keys())
    workspace = [
        w
        for w in workspace
        if not any(fnmatch.fnmatch(w, pat) for pat in exclude.get("Workspace", []))
    ]

    # Map src to tgt fields
    fields = {}
    all_fields["Text"] = TEXT
    for fmapping in [default_mapping, field]:
        tfields = {}
        for mapping in fmapping:
            src, tgt, *conv = mapping.split(":") if ":" in mapping else (mapping, mapping)
            tfields.setdefault(src, []).append((all_fields.get(tgt), conv[0] if conv else None))
        fields.update(tfields)

    # TODO: need a more general way to create fields, or get rid of the idea of creating fields
    # TODO: problem with creating singleselect fields is there is no api to add extra options later
    if "Workspace" in fields and fields["Workspace"] is None:
        # grey = gh_query.__self__.schema.type_map['ProjectV2SingleSelectFieldOptionColor'].values['GRAY']
        options = [
            dict(fuzzy_get(workspaces, name), description="", color="GRAY") for name in workspace
        ]
        field = gh_query(gh_add_field, dict(name="Workspace", proj=proj["id"], options=options))
        field["Workspace"] = fields["Workspace"] = field

    stats = dict(removed=0, text=0, added=0)
    seen = {}
    last = {}
    for name in workspace:
        ws = fuzzy_get(workspaces, name)
        stats['added'] += sync_workspace(ws, proj, fields, items, exclude, seen, last, zh_query, gh_query, **args)

    # We need to set the body on any that we changed
    print("Save text changes")
    for item in items.values():
        if not item['content'] or '_deps' not in item['content']:
            continue
        updated = set_text(item['content'], gh_query)
        print(f"- '{item['content']['title']}' - {'UPDATED' if updated else 'SKIPPED'}")
        stats['text'] += 1 if updated else 0

    # get list of all items in the current project so we can remove ones added by mistake if desired.
    print()
    print(f"Can remove - items no longer in input")
    print("======================================")
    all_items = set(
        (r, i.get("content", {}).get("title"), i["id"]) for r, i in items.items() if "id" in i
    )
    added_items = set((r, i.get("content", {}).get("title"), i["id"]) for r, i in seen.items())
    for repo, name, item in all_items - added_items:
        if not repo:
            print(f"- '{repo[0]}':'{name}' - NOT REMOVED - Draft Issue")
        elif args["disable_remove"]:
            print(f"- '{repo[0]}':'{name}' - NOT REMOVED - --disable-remove=true")
        else:
            gh_query(gh_del_item, dict(proj=proj["id"], issue=item))
            print(f"- '{repo[0]}':'{name}' - REMOVED")
            stats['removed'] += 1
    else:
        print("- None")
    return stats


def sync_workspace(ws, proj, fields, items, exclude, seen, last, zh_query, gh_query, **args):
    # TODO: we aren't syncing data on closed tickets that were part of the workspace,
    # - would be send these to a special closes status or just remove them from the project?

    added = 0

    epics = {
        e["issue"]["id"]: e
        for e in zh_query(zh_get_epics, dict(workspaceId=ws["id"]))["workspace"]["epics"]["nodes"]
    }

    deps = {}
    for i in zh_query(zh_get_dep, dict(workspaceId=ws["id"]))["workspace"]["issueDependencies"][
        "nodes"
    ]:
        blocked = deps.setdefault(i["blockedIssue"]["id"], [])
        if i["blockingIssue"] not in blocked:
            blocked.append(i["blockingIssue"])

    statuses = {opt["name"]: opt for opt in fields["Pipeline"][0][0]["options"]}

    for pos, pipeline in enumerate(ws["pipelines"]):
        # supposed to include prs also even if linked but doesn't seem to.
        issues = zh_query(
            zh_issues, dict(pipelineId=pipeline["id"], workspaceId=ws["id"])
        )["searchIssuesByPipeline"]["nodes"]
        prs = zh_query(
            zh_prs, dict(pipelineId=pipeline["id"], workspaceId=ws["id"])
        )["searchIssuesByPipeline"]["nodes"]
        prs = {pr['id']: pr for pr in prs}

        if any(fnmatch.fnmatch(pipeline["name"], drop_col) for drop_col in exclude.get("Pipeline", [])):
            print(f"Excluding Pipeline '{ws['name']}/{pipeline['name']}'")
            continue

        # add state if we don't have it. # TODO. no api for this yet. Have to create the whole field
        # for now we will just pick closest match
        status = fuzzy_get(statuses, pipeline["name"])
        print(f"Merging {ws['name']}/{pipeline['name']} -> {proj['title']}/{status['name']}")
        for issue in issues + list(prs.values()):
            issue["Pipeline"] = pipeline["name"]
            changes = []
            issue['connections'] = prs.get(issue['id'], {'connections': {}})['connections']
            key = issue_key(issue)
            if key in items:
                item = items[key]
                gh_issue = item["content"]
            else:
                gh_issue = get_issue(gh_query, items, issue=issue)
                item = None

            if gh_issue["repository"]["archivedAt"] is not None:
                # handle if the issue is in archived repo
                print(
                    f"- '{issue['repository']['name']}':'{issue['title']}' - SKIP - Archived Repo"
                )
                continue
            elif zh_value(ws, issue, "Linked Issues", epics, deps, zh_query)[0]:
                # Any PR that is linked we don't want to appear on the board
                # If linked correctly then it will appear be merged in with the issue
                # If not gh linked (e.g. not main branch PR), then we want to hide it since it's not on ZH Board
                # TODO: this does mean any fields set on the item will not be transfered
                # - linked PR won't appear in the board but does it appear in other views if added?
                # - to fix we'd have to only skip PR's that we can't link.
                print(
                    f"- '{issue['repository']['name']}':'{issue['title']}' - SKIP - don't add linked PRs"
                )
                item = {}  # We still want to link it to the Issue and set non project stuff
            elif item is None:
                # add issue if not there
                item = gh_query(gh_add_item, dict(proj=proj["id"], issue=gh_issue["id"]))[
                    "addProjectV2ItemById"
                ]["item"]
                items[key] = item
                item["content"] = gh_issue  # Don't need to get this again via the query
                # Need to get the board from somewhere. TODO: switch board and last
                cache_init_board(item, next(iter(last.values()), None))
                changes += ["ADD*"]
                added += 1
            if key in seen:
                print(f"- '{issue['repository']['name']}':'{issue['title']}' - SKIP - Added already")
                continue
            elif item:
                seen[key] = item

            # # TODO: can we reproduce the history?

            changes = []
            for src, dst in fields.items():
                for field, conv in dst:
                    res = []
                    closest = conv.lower() if conv and conv.lower() in ['closest', 'exact', 'scale'] else 'closest'
                    if src == 'Position':
                        value, options = last[status['id']]["id"] if last.get(status['id']) else None, []
                    else:
                        value, options = zh_value(ws, issue, src, epics, deps, zh_query)
                    if item and field and field.get('name') == 'Position':
                        # set order/position
                        # TODO: there is a way with less moves
                        if cache_is_after(item, value):
                            changes += []
                        else:
                            gh_query(
                                gh_set_order,
                                dict(
                                    proj=proj["id"],
                                    item=item["id"],
                                    after=value,
                                ),
                            )
                            changes += ["POS*"] if value else ["TOP*"]
                            cache_after(item, value)             
                    elif not value or not field:
                        # TODO: we should unset the field?
                        continue
                    elif type(value) == list:
                        # List of issues. Currently no support for multi-select fields
                        if field == TEXT:
                            text = ""
                            fixes = "fixes " if src == 'Linked Issues' else ""
                            for sub in value:
                                text += f"- [ ] {fixes}{shorturl(sub['url'])}\n"
                            res.append(add_text(gh_issue, src, text, proj))
                        elif field["name"] == "Linked pull requests":
                            res.extend(update_linked_prs(item, gh_issue, field, value, proj))
                        else:
                            # only other way to record this is by
                            # setting a field value on the linked item
                            for sub in value:
                                sub = get_issue(gh_query, items, sub["url"])
                                res.append(
                                    set_field(
                                        proj, sub, field, gh_issue["title"], gh_query, closest, options
                                    )
                                )
                    elif field == TEXT:
                        text += f"- {value}\n"
                        res.append(add_text(issue, src, text, proj))
                    elif not item:
                        pass
                    else:
                        # TODO: if it's a PR that ZH thinks is linked but github doesn't like the link (ie not PR to main). 
                        # Then maybe reset status to None so doesn't appear in the project?
                        # or if that doesn't work, never add it
                        res.append(set_field(proj, item, field, value, gh_query, closest, options))
                    if res:
                        changes += [f"{field.get('name', src)[:3].upper()}{'*' if any(res) else ''}"]
            if item:
                last[status['id']] = item

            print(f"- '{issue['repository']['name']}':'{issue['title']}' - {', '.join(changes)}")
    return added


def fuzzy_get(dct, key, closest=True, globs=False):
    """
    return the value whose key is the closest

      >>> fuzzy_get({"foo":1, "bah":2}, "fo")
      1
    """
    if type(dct) == list:
        dct = {i: i for i in dct}
    if globs:
        res = next((fnmatch.fnmatch(name, key) for name in dct.keys()), None)
        if res is not None:
            dct[res]
    if closest:
        res = next(iter(difflib.get_close_matches(key, dct.keys(), 1, 0)))
    else:
        res = key
    return dct.get(res, None)


def get_issue(gh_query, items, url=None, owner=None, repo=None, number=None, issue=None):
    if url is not None:
        prot, _, gh, owner, repo, _type, number = url.split("/")[:7]
    elif issue is not None:
        number = issue["number"]
        repo = issue["repository"]["name"]
        owner = issue["repository"]["owner"]["login"]

    if (owner, repo, number) in items:
        return items[(owner, repo, number)]["content"]

    issue = gh_query(
        gh_get_issue,
        dict(
            number=int(number),
            repo=repo,
            owner=owner,
        ),
    )[
        "repository"
    ]["issueOrPullRequest"]

    # put in a fake item for now
    items[(owner, repo, number)] = dict(content=issue)
    return issue


def zh_value(ws, issue, name, epics, deps, zh_query):
    if name == "Workspace":
        # put in original workspace name as custom field
        # - we can always get rid of it later
        # - we can automate setting it later based on repo
        # - allows us to set client on external issues
        return ws["name"], []  # TODO: get the list of all workspaces
    elif name == "Pipeline":
        return issue["Pipeline"], [p["name"] for p in ws["pipelines"]]
    elif name == "Estimate":
        story_points = [40, 21, 13, 8, 5, 3, 2, 1]
        # TODO: can't find in graph api how to get this scale. or find a better hack?
        # - could get all items and find range that way?

        # if aren't using this scale then we will do a hack and just insert the new value in so it works out
        value = issue["estimate"]["value"] if issue["estimate"] else None
        if value is not None and value not in story_points:
            story_points = list(sorted(story_points + [value], reverse=True))
        return value, story_points

    elif name == "Priority":
        return issue["pipelineIssue"]["priority"]["name"] if issue["pipelineIssue"][
            "priority"
        ] else None, ["Normal", "High Priority"]
    elif name == "Sprints":
        # Can't really set multiple iterations so just use latest
        return issue["sprints"]["nodes"][-1], []
    # Reading the history turns out to be unreliable. Instead we will set this on the linked PRs.
    # elif name == "PR":
    #     urls = zh_history_values(issue, "issue.connect_issue_to_pr", "issue.disconnect_issue_from_pr")
    #     return [u for u in urls if "/oull/" in u], []
    elif name == "Linked Issues" and issue['pullRequest']:
        # urls = zh_history_values(issue, "issue.connect_issue_to_pr", "issue.disconnect_issue_from_pr")
        # Issue is a PR and links back to a ticket/issue
        return [dict(url="https://github.com/{}/{}/issue/{}".format(*issue_key(linked))) for linked in issue["connections"]["nodes"]], []
    elif name == "Epic":
        if issue["id"] not in epics:
            return None, []
        urls = []
        epic = epics[issue["id"]]
        # # TODO: This info won't get transfered if the epic itsself has been closed.
        # - might need an extra step to transfer information for closed tickets that were part of the workspace?
        for subissue in zh_query(
            zh_epic_issues, dict(zenhubEpicId=epic["id"], workspaceId=ws["id"])
        )["node"]["childIssues"]["nodes"]:
            urls.append(dict(url=subissue["htmlUrl"]))
        return urls, []
    elif name == "Blocked By" and issue["id"] in deps:
        return [dict(url=i["htmlUrl"]) for i in deps.get(issue["id"], [])], []
    else:
        return (None, [])


def zh_history_values(issue, add_event, rm_event):
    # Use the history in ZH to work out the value of some fields there is no api for
    urls = []
    for htype, hist in [
        (hist["type"], hist["data"])
        for hist in issue["timelineItems"]["nodes"]
        if hist["type"] in [add_event, rm_event]
    ]:
        if "pull_request" in hist:
            itype, owner, repo, number = (
                "pull",
                hist["pull_request_organization"]["login"],
                hist["pull_request_repository"]["name"],
                hist["pull_request"]["number"],
            )
        else:
            itype, owner, repo, number = (
                "issue",
                hist["issue_organization"]["login"],
                hist["issue_repository"]["name"],
                hist["issue"]["number"],
            )
        url = f"https://github.com/{owner}/{repo}/{itype}/{number}"
        if htype == add_event:
            urls.append(dict(url=url))
        else:
            urls.remove(dict(url=url))
    return urls
    

def field_value(item, field):
    value = next(
        (n for n in item["fieldValues"]["nodes"] if n.get("field", {}).get("id") == field["id"]),
        None,
    )
    if value is None:
        return None
    else:
        return next(
            value[at]
            for at in ["text", "number", "pullRequests", "optionId", "users"]
            if at in value
        )


def cache_is_after(item, after):
    """ True if the item is already after "after" in the target board """
    board, status = item['_board']
    col = board[status]
    i = col.index(item)
    i_after = next((pos for pos, i in enumerate(col) if i['id'] == after)) if after else -1
    return i == i_after + 1


def cache_init_board(new_item, item=None, board={}):
    """" set the shared board state on the item. Set status to None ready to put onto the cached board """
    board, _ = item['_board'] if item is not None else (board, None)
    new_item['_board'] = (board, None)  # actual location will be set later


def cache_after(item, after):
    """ move item to after "after" in the target board cache """
    board, status = item['_board']
    col = board[status]
    col.remove(item)
    i_after = next((pos for pos, i in enumerate(col) if i['id'] == after)) if after else -1
    col.insert(i_after + 1, item)


def cache_after_new(item, new_status):
    """ put item at the bottom of the new_status col on the cached target board """
    board, status = item['_board']
    old_col = board.get(status, [])
    if item in old_col:
        old_col.remove(item)
    board.setdefault(new_status, []).append(item)
    item['_board'] = (board, new_status)


field_stats = {}


def set_field(proj, item, field, value, gh_query, match="closest", options=[]):
    orig_value = value
    if match == "scale" and "options" in field and options:
        # work out closest by rank
        pos = options.index(value)
        new_opt = field["options"]
        value = new_opt[round(pos / len(options) * len(new_opt))]["id"]
    elif "options" in field and value not in {opt["id"] for opt in field["options"]}:
        # Get closest match
        value = fuzzy_get({opt["name"]: opt for opt in field["options"]}, value, closest=match == 'closest')
        value = value["id"] if value else None
    if "options" in field:
        vname = next((f['name'] for f in field['options'] if f['id'] == value), None)
        mapping = field_stats.setdefault(field['name'], {})
        mapping.setdefault((orig_value, vname), 0)
        mapping[(orig_value, vname)] += 1
    if field_value(item, field) == value:
        return False
    elif value is None:
        gh_query(gh_del_value, dict(proj=proj["id"], item=item["id"], field=field["id"]))
    else:
        query = (
            gh_set_option
            if "options" in field
            else gh_set_number
            if type(value) == int or float
            else gh_set_value
        )
        gh_query(query, dict(proj=proj["id"], item=item["id"], field=field["id"], value=value))
        if field["name"] == 'Status':
            # need to keep track of positin
            cache_after_new(item, value)
    return True


def update_linked_prs(item, gh_issue, field, value, proj):
    res = []
    # We can't set this directly. Only by modifying each PR text.
    # # TODO: there is special linked PR field but can't set it? https://github.com/orgs/community/discussions/40860
    # # seems like it only lets you create a branch, not link a PR? do via discussion? or work out how UI lets you set it?
    # # gh_query(gh_set_value, dict(proj=proj, item=item['id'], field=fields['Linked pull requests']['id'], value=pr['url']))
    for sub in value:
        # Check not already linked
        linked = field_value(item, field)
        if linked and any(
            pr["url"] == sub["url"] for pr in linked["nodes"]
        ):
            continue
        if not same_org(gh_issue, sub):
            # Let's skip modifying others PR's
            print(
                f"- '{gh_issue['title']}' - SKIP PR update on '{sub['url']}'"
            )
            continue
        # TODO: this will only work if the base is the main branch. Linked field won't get updated
        sub = get_issue(gh_query, items, sub["url"])
        res.append(
            add_text(
                sub,
                "Linked Issues",
                f"- Fixes {shorturl(gh_issue['url'])}",
                proj
            )
        )
    return res
    

def same_org(gh_issue, sub):
    if "owner" in sub:
        # It's a project
        return sub['owner']['login'] == gh_issue['repository']['owner']['login']
    else:
        # another issue
        return gh_issue["url"].split("/")[:4] == sub["url"].split("/")[:4]


def add_text(gh_issue, name, text, proj):

    # Add the text in there and combine at the end
    deps = gh_issue.setdefault("_deps", {})

    if not same_org(gh_issue, proj):
        # Remove or text if not same org
        print(
            f"- '{gh_issue['title']}' - SKIP TEXT update on '{gh_issue['url']} - different org'"
        )
        return False

    checklist = deps.setdefault(name, [])
    if text not in checklist:
        # Some linked PR cna be duplicated
        checklist.append(text)
        return True


def set_text(gh_issue, gh_query, heading="Dependencies"):
    if "_deps" not in gh_issue:
        return False
    old_body = get_issue(gh_query, {}, gh_issue['url'])["body"]
    body, *rest = old_body.split(f"\r\n# {heading}\r\n", 1)
    rest = "" if not rest else rest[0]
    if "\n# " in rest:
        rest = rest[rest.find("\r\n# ") :]
    else:
        rest = ""
    text = ""
    for title, lines in gh_issue.get("_deps", {}).items():
        if lines:
            text += f"\n## {title}\n"
        for line in lines:
            text += f"\n{line}"
    if text:
        text = (f"\n# {heading}\n" + text).replace("\n", "\r\n")
    new_body = body + text + rest

    if old_body != new_body:
        gh_query(
            gh_setpr_body if "/pull/" in gh_issue["url"] else gh_set_body,
            dict(id=gh_issue["id"], body=new_body),
        )
        gh_issue["body"] = new_body
        return True
    else:
        return False


def issue_key(issue):
    # Can be a DRAFT_ISSUE which has no content
    return (issue["repository"]["owner"]["login"], issue["repository"]["name"], issue["number"]) if issue else None


def shorturl(url, base=None):
    return url.replace("https://github.com/", "").replace("/issue/", "#").replace("/pull/", "#")


zh_workspace = gql(
    """
query ($workspaceId: ID!) {
    workspace(workspaceId: $workspaceId) {
        id
        displayName
        pipelines {
            id
            name
        }
    }
}
"""
)

zh_workspaces = gql(
    """
query RecentlyViewedWorkspaces {
    recentlyViewedWorkspaces {
        nodes {
            name
            id
            pipelines {
                id
                name
            }
        }
    }
}
"""
)

# Supposed to include PRs too but doesn't seem to? maybe only linked ones missing?
zh_issues = gql(
    """
query ($pipelineId: ID!, $workspaceId:ID!) {
    searchIssuesByPipeline(pipelineId: $pipelineId, filters:{displayType: all} ) {
        nodes {
            id
            title
            ghId
            number
            pullRequest
            pipelineIssue(workspaceId: $workspaceId) {
              priority {
                id
                name
                color
              }
            }
            timelineItems(first: 50) {
              nodes {
                type: key
                id
                data
                createdAt
              }
            }
            repository {
                id
                ghId
                name
                owner {
                  id
                  ghId
                  login
                }
            }
            estimate {
                value
            }
            sprints(first: 10) {
                nodes {
                    id
                    name
                }
            }
        }
    }
}
"""
)

zh_prs = gql(
    """
query ($pipelineId: ID!, $workspaceId:ID!) {
    searchIssuesByPipeline(pipelineId: $pipelineId, filters:{displayType: prs} ) {
        nodes {
            id
            title
            ghId
            number
            pullRequest
            pipelineIssue(workspaceId: $workspaceId) {
              priority {
                id
                name
                color
              }
            }
            repository {
                id
                ghId
                name
                owner {
                  id
                  ghId
                  login
                }
            }
            estimate {
                value
            }
            sprints(first: 10) {
                nodes {
                    id
                    name
                }
            }
            connections(first:20) {
              nodes {
                id
                title
                ghId
                number
                repository {
                    id
                    ghId
                    name
                    owner {
                      id
                      ghId
                      login
                    }
                }
              }
            
            }
        }
    }
}
"""
)


zh_get_epics = gql(
    """
    query getEpicsById($workspaceId: ID!) {
      workspace(id: $workspaceId) {
        id
        epics(first:100) {
          nodes {
            id
            issue {
              id
              title
              number
              htmlUrl
              repository {
                id
                name
              }
            }
          }
        }
      }
    }
"""
)


zh_epic_issues = gql(
    """
query Issues($zenhubEpicId: ID!) {
      node(id: $zenhubEpicId) {
        ... on Epic {
          childIssues {
              nodes { 
                     id 
                     title 
                     number
                     htmlUrl
                     repository {
                        id
                        name
                      }
                     } 
          } 
        } 
      } 
}
"""
)


zh_get_dep = gql(
    """
    query getWorkspaceDependencies($workspaceId: ID!) {
      workspace(id: $workspaceId) {
        issueDependencies(first: 50) {
          nodes {
            id
            updatedAt
            blockedIssue {
              id
              number
              htmlUrl
              repository {
                id
                ghId
              }
            }
            blockingIssue {
              id
              number
              htmlUrl
              repository {
                id
                ghId
              }
            }
          }
        }
      }
    }
"""
)


gh_add_column = gql(
    """
    mutation ($name: String!, $projectId: ID!) {
        addProjectColumn(input: {
            name: $name, projectId: $projectId
        }) {
            columnEdge
        }
    }
"""
)

gh_get_states = gql(
    """
  mutation($proj:String, $item:String, $field:String, $option:String ) {
    updateProjectV2ItemFieldValue(
      input: {
        projectId: $proj
        itemId: $item
        fieldId: $field
        value: { 
          singleSelectOptionId: $option        
        }
      }
    ) {
      projectV2Item {
        id
      }
    }
  }
                    """
)

gh_add_project = gql(
    """
mutation ($ownerId: ID!, $repositoryId: ID!, $teamId: ID!, $title: String!) {
    createProjectV2(input: {ownerId: $ownerId, repositoryId: $repositoryId, teamId: $teamId, title: $title}) {
        projectV2 {
            number
            resourcePath
            url
        }
    }
}
"""
)

gh_user = gql(
    """
{
  viewer {
    login
    id
  }
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
}
"""
)

gh_org = gql(
    """
  query($login:String!, $number:Int!) {
    organization(login:$login) {
      id
      projectV2(number: $number) {
        title
        url
        id
        number
        owner {
          ... on Organization {
            login
          }
        }
        teams(first:100) { 
          nodes {
            id
          }
        }
        creator { login }
        public
        repositories(first:100) {
          nodes {
            id
          }
        }
      } 
    }
}
"""
)


gh_proj_items = gql(
    """
  query($login:String!, $number:Int!, $cursor:String) {
    organization(login:$login) {
      projectV2(number: $number) {
        items(first:100, after:$cursor, orderBy: {field: POSITION, direction: ASC}) {
          pageInfo {
              hasNextPage
              endCursor
          }
          nodes {
            id
            content {
                ... on Issue {
                  title
                  url
                  id
                  number
                  repository {id name archivedAt owner{login }}
                }
                ... on PullRequest {
                  title
                  url
                  id
                  number
                  repository {id name archivedAt owner{login }}
                }
            }
            databaseId
            fieldValues(first:100) {
              nodes {
                ... on ProjectV2ItemFieldValueCommon { field {... on ProjectV2FieldCommon { id }} }
                ... on ProjectV2ItemFieldSingleSelectValue { optionId  }
                ... on ProjectV2ItemFieldTextValue { text  }
                ... on ProjectV2ItemFieldUserValue { users(first:10) { nodes { login }} field {... on Node { id } } }
                ... on ProjectV2ItemFieldNumberValue { number }
                ... on ProjectV2ItemFieldPullRequestValue { pullRequests(first:10) { nodes { id url }} field {... on Node { id } } }
                ... on ProjectV2ItemFieldDateValue { date }
              }
            }
            type
          }
        }
      } 
    }
}
"""
)


gh_add_item = gql(
    """
  mutation ($proj: ID!, $issue: ID!) {
    addProjectV2ItemById(input: {
            projectId: $proj, contentId: $issue
    }) {
      item {
        id
            fieldValues(first:100) {
              nodes {
                ... on ProjectV2ItemFieldValueCommon { field {... on ProjectV2FieldCommon { id }} }
                ... on ProjectV2ItemFieldSingleSelectValue { optionId  }
                ... on ProjectV2ItemFieldTextValue { text  }
              }
            }
      }
    }
  }
"""
)


gh_del_item = gql(
    """
  mutation ($proj: ID!, $issue: ID!) {
    deleteProjectV2Item(input: {
            projectId: $proj, itemId: $issue
    }) {
                  deletedItemId
    }
  }
"""
)


gh_get_issue = gql(
    """
query($repo:String!, $owner:String!, $number:Int!) {
  repository(name:$repo, owner:$owner) {
      issueOrPullRequest(number: $number) {
        ... on Issue {
          title
          url
          id
          number
          body
          repository {id name archivedAt owner{login }}
          projectsV2(first:100) { nodes { id }
          }
        }
        ... on PullRequest {
          title
          url
          id
          number
          body        
          repository {id name archivedAt owner{login }}
          projectsV2(first:100) { nodes { id }
          }
        }
      }
  }
}
"""
)

gh_get_pr = gql(
    """
query($repo:String!, $owner:String!, $number:Int!) {
  repository(name:$repo, owner:$owner) {
      pullRequest(number: $number) {
          title
          url
          id
          number
          body
        }
  }
}
"""
)

gh_add_field = gql(
    """
mutation($proj:ID!, $name:String!, $options:[ProjectV2SingleSelectFieldOptionInput!]) {
    createProjectV2Field(
              input: { 
                   dataType:SINGLE_SELECT 
                   name:$name 
                   projectId:$proj 
                   singleSelectOptions:$options
                   }
                   ) {
                   projectV2Field {
                    ... on ProjectV2SingleSelectField {
                      id
                      name
                      options {
                        id
                        name
                      }
                    }
                   }
    }  
}
"""
)

gh_get_Fields = gql(
    """
  query($proj:ID!){
  node(id: $proj) {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2Field {
            id
            name
          }
          ... on ProjectV2IterationField {
            id
            name
            configuration {
              iterations {
                startDate
                id
              }
            }
          }
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""
)


gh_set_body = gql(
    """
  mutation($id:ID!, $body:String!) {
    updateIssue(
      input: {
        id: $id
        body: $body
      }
    ) {
      issue {
        id
      }
    }
  }
"""
)

gh_setpr_body = gql(
    """
  mutation($id:ID!, $body:String!) {
    updatePullRequest(
      input: {
        pullRequestId: $id
        body: $body
      }
    ) {
      pullRequest {
        id
      }
    }
  }
"""
)

gh_set_value = gql(
    """
  mutation($proj:ID!, $item:ID!, $field:ID!, $value:String) {
    updateProjectV2ItemFieldValue(
      input: {
        projectId: $proj
        itemId: $item
        fieldId: $field
        value: { 
          text: $value       
        }
      }
    ) {
      projectV2Item {
        id
      }
    }
  }
"""
)

gh_set_number = gql(
    """
  mutation($proj:ID!, $item:ID!, $field:ID!, $value:Float) {
    updateProjectV2ItemFieldValue(
      input: {
        projectId: $proj
        itemId: $item
        fieldId: $field
        value: { 
          number: $value       
        }
      }
    ) {
      projectV2Item {
        id
      }
    }
  }
"""
)

gh_set_option = gql(
    """
  mutation($proj:ID!, $item:ID!, $field:ID!, $value:String) {
    updateProjectV2ItemFieldValue(
      input: {
        projectId: $proj
        itemId: $item
        fieldId: $field
        value: { 
          singleSelectOptionId: $value       
        }
      }
    ) {
      projectV2Item {
        id
      }
    }
  }
"""
)

gh_del_value = gql(
    """
  mutation($proj:ID!, $item:ID!, $field:ID!) {
    clearProjectV2ItemFieldValue(
      input: {
        projectId: $proj
        itemId: $item
        fieldId: $field
      }
    ) {
      projectV2Item {
        id
      }
    }
  }
"""
)

gh_set_order = gql(
    """
  mutation($proj:ID!, $item:ID!, $after:ID) {
    updateProjectV2ItemPosition(
      input: {
        projectId: $proj
        itemId: $item
        afterId: $after
      }
    ) {
      items(first:100) {
        nodes {
                   id
        }
      }
    }
  }
"""
)


def main():
    # turn my docopt args into python args
    args = {k.strip("<>-- ").replace("-", "_").lower(): v for k, v in docopt(__doc__).items()}
    stats = merge_workspaces(**args)

    print()
    print("Summary")
    print("=======")
    print("Added: {added}, Removed:{removed}, Text Changes:{text}".format(**stats))
    print()

    for fieldname, values in field_stats.items(): 
        print(fieldname)
        for from_to, count in values.items():
            _from, to = from_to
            print(f"\t{_from} -> {to}: {count}")



if __name__ == "__main__":
    main()
