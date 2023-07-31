"""Projects Migrator: Sync Zenhub workspaces into a single Github Project

Usage:
  projectsmigrator.py PROJECT_URL [--workspace=NAME]... [--exclude=FIELD:PATTERN]... [--field=SRC:DST]... [options]
  projectsmigrator.py (-h | --help)

Options:
  -w=NAME, --workspace=NAME            Name of a Zenhub workspace to import or none means include all.
  -f=SRC:DST:CNV, --field=SRC:DST:CNV  Transfer SRC field to DST field. "Text" as DST will add a checklist
                                       for Epic and Blocking issues, and values into the text for other fields.
                                       CNV "Scale" (match by rank), Exact or Closest (default).
                                       One SRC can have many DST fields.
                                       [Default: Estimate:Size:Scale, Priority:Priority, Pipeline:Status,
                                       PR:Linked Pull Requests, Epic:Text, Blocking:Text, Sprint:Iteration]
                                       "SRC:" Will not transfer this field
  -x=FIELD:PAT, --exclude=FIELD:PAT    Don't include issues with field values that match the pattern
                                       e.g. "Workspace:Private*", "Pipeline:Done".
  --disable-remove                     Project items not found in any of the workspace won't be removed.
  --github-token=<token>               or use env var GITHUB_TOKEN.
  --zenhub-token=<token>               or use env var ZENHUB_TOKEN.
  -h, --help                           Show this screen.

For zenhub the following fields are available.
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
    "PR:Linked pull requests",
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
            timeout=60,
        ),
        fetch_schema_from_transport=True,
        serialize_variables=True,
    ).execute
    token = os.environ["GITHUB_TOKEN"] if not args["github_token"] else args["github_token"]
    gh_query = Client(
        transport=AIOHTTPTransport(
            url="https://api.github.com/graphql",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        ),
        fetch_schema_from_transport=False,
        serialize_variables=True,
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

    # Get all the items so we can speed up queries and reduce updates
    items = []
    cursor = None
    print("Reading Project", end="")
    while True:
        res = gh_query(gh_proj_items, dict(login="pretagov", number=int(proj_num), cursor=cursor))[
            "organization"
        ]["projectV2"]["items"]
        items.extend(res["nodes"])
        print(".", end="")
        if not res["pageInfo"]["hasNextPage"]:
            break
        cursor = res["pageInfo"]["endCursor"]
    print()

    # Record order so we can see if it needs moving
    board = {}
    for i in items:
        status = field_value(i, all_fields['Status'])
        col = board.setdefault(status, [])
        i['after'] = col[-1] if col else {}
        col.append(i)
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

    if "Workspace" in fields and fields["Workspace"] is None:
        # grey = gh_query.__self__.schema.type_map['ProjectV2SingleSelectFieldOptionColor'].values['GRAY']
        options = [
            dict(fuzzy_get(workspaces, name), description="", color="GRAY") for name in workspace
        ]
        field = gh_query(gh_add_field, dict(name="Workspace", proj=proj["id"], options=options))
        field["Workspace"] = fields["Workspace"] = field
        # TODO: add in missing options

    seen = {}
    last = {}
    for name in workspace:
        ws = fuzzy_get(workspaces, name)
        sync_workspace(ws, proj, fields, items, exclude, seen, last, zh_query, gh_query, **args)

    # We need to set the body on any that we changed
    print("Save text changes")
    for item in items.values():
        action = 'UPDATED' if set_text(item['content'], gh_query) else 'SKIPPED'
        print(f"- '{item['content']['title']}' - {action}")

    # get list of all items in the current project so we can remove ones added by mistake if desired.
    if not args["disable_remove"]:
        print(f"Not in any workspace")
        all_items = set(
            (r, i.get("content", {}).get("title"), i["id"]) for r, i in items.items() if "id" in i
        )
        added_items = set((r, i.get("content", {}).get("title"), i["id"]) for r, i in seen.items())
        for repo, name, item in all_items - added_items:
            gh_query(gh_del_item, dict(proj=proj["id"], issue=item))
            print(f"- '{repo[0]}':'{name}' - REMOVED")


def sync_workspace(ws, proj, fields, items, exclude, seen, last, zh_query, gh_query, **args):
    # TODO: we aren't syncing data on closed tickets that were part of the workspace,
    # - would be send these to a special closes status or just remove them from the project?

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
        issues = zh_query(
            zh_issues, dict(pipelineId=pipeline["id"], filters={}, workspaceId=ws["id"])
        )["searchIssuesByPipeline"]["nodes"]

        if any(fnmatch.fnmatch(pipeline["name"], drop_col) for drop_col in exclude["Pipeline"]):
            print(f"Excluding Pipeline '{ws['name']}/{pipeline['name']}'")
            continue

        # add state if we don't have it. # TODO. no api for this yet. Have to create the whole field
        # for now we will just pick closest match
        status = fuzzy_get(statuses, pipeline["name"])
        print(f"Merging {ws['name']}/{pipeline['name']} -> {proj['title']}/{status['name']}")
        for issue in issues:
            issue["Pipeline"] = pipeline["name"]
            changes = []
            key = issue_key(issue)
            if key in items:
                item = items[key]
                gh_issue = item["content"]
            else:
                gh_issue = get_issue(
                    gh_query,
                    items,
                    number=issue["number"],
                    repo=issue["repository"]["name"],
                    owner=issue["repository"]["owner"]["login"],
                )
                item = None

            if gh_issue["repository"]["archivedAt"] is not None:
                # handle if the issue is in archived repo
                print(
                    f"- '{issue['repository']['name']}':'{issue['title']}' - Archived Repo - SKIP"
                )
                continue
            elif item is None:
                # add issue if not there
                item = gh_query(gh_add_item, dict(proj=proj["id"], issue=gh_issue["id"]))[
                    "addProjectV2ItemById"
                ]["item"]
                items[key] = item
                item["content"] = gh_issue  # Don't need to get this again via the query
                changes += ["ADD*"]
            if key in seen:
                print(f"- '{issue['repository']['name']}':'{issue['title']}' - SKIP")
                continue
            else:
                seen[key] = item

            # # TODO: can we reproduce the history?

            changes = []
            for src, dst in fields.items():
                for field, conv in dst:
                    res = []
                    closest = conv.lower() != "exact" if conv else True
                    if src == 'Position':
                        value, options = last[status['id']]["id"] if last.get(status['id']) else None, []
                    else:
                        value, options = zh_value(ws, issue, src, epics, deps, zh_query)
                    if field and field.get('name') == 'Position':
                        # set order/position
                        if item.get("after", {}).get("id") == value and not last.get(status['id'], {}).get("_moved"):
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
                            item['_moved'] = True             
                    elif not value or not field:
                        # TODO: we should unset the field?
                        continue
                    elif type(value) == list:
                        # List of issues. Currently no support for multi-select fields
                        if field == TEXT:
                            text = ""
                            for sub in value:
                                text += f"- [ ] {shorturl(sub['url'])}\n"
                            res.append(add_text(gh_issue, src, text))
                        elif field["name"] == "Linked pull requests":
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
                                if gh_issue["url"].split("/")[:4] != sub["url"].split("/")[:4]:
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
                                    )
                                )
                        else:
                            # only other way to record this is by
                            # setting a field value on the linked item
                            for sub in value:
                                sub = get_issue(gh_query, items, sub["url"])
                                res.append(
                                    set_field(
                                        proj, sub, field, gh_issue["title"], gh_query, closest
                                    )
                                )
                    elif conv and conv.lower() == "scale" and "options" in field and options:
                        # work out closest by rank
                        pos = options.index(value)
                        new_opt = field["options"]
                        new_value = new_opt[round(pos / len(options) * len(new_opt))]["id"]
                        res.append(set_field(proj, item, field, new_value, gh_query))
                    else:
                        res.append(set_field(proj, item, field, value, gh_query, closest))
                    if res:
                        changes += [f"{field.get('name', src)[:3].upper()}{'*' if any(res) else ''}"]

            last[status['id']] = item

            print(f"- '{issue['repository']['name']}':'{issue['title']}' - {', '.join(changes)}")


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


def get_issue(gh_query, items, url=None, owner=None, repo=None, number=None):
    if url is not None:
        prot, _, gh, owner, repo, _type, number = url.split("/")[:7]

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
    elif name == "PR":
        urls = []
        for hist in [
            hist["data"]
            for hist in issue["timelineItems"]["nodes"]
            if "issue.connect_issue_to_pr" in hist["type"]
        ]:
            powner, prepo, pnumber = (
                hist["pull_request_organization"]["login"],
                hist["pull_request_repository"]["name"],
                hist["pull_request"]["number"],
            )
            prurl = f"https://github.com/{powner}/{prepo}/pull/{pnumber}"
            urls.append(dict(url=prurl))
        return urls, []
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


def set_field(proj, item, field, value, gh_query, closest=True):
    if "options" in field and value not in {opt["id"] for opt in field["options"]}:
        # Get closest match
        value = fuzzy_get({opt["name"]: opt for opt in field["options"]}, value, closest=closest)
        value = value["id"] if value else None
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
    return True


def add_text(gh_issue, name, text):
    # Add the text in there and combine at the end
    checklist = gh_issue.setdefault("_deps", {}).setdefault(name, [])
    if text not in checklist:
        # Some linked PR cna be duplicated
        checklist.append(text)


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
    return (issue["repository"]["owner"]["login"], issue["repository"]["name"], issue["number"])


def shorturl(url, base=None):
    return url.replace("https://github.com/", "").replace("/issues/", "#").replace("/pull/", "#")


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


zh_issues = gql(
    """
query ($pipelineId: ID!, $filters: IssueSearchFiltersInput!, $workspaceId:ID!) {
    searchIssuesByPipeline(pipelineId: $pipelineId, filters:$filters ) {
        nodes {
            id
            title
            ghId
            number
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
    args = {k.strip("<>-- ").replace("-", "_").lower(): v for k, v in docopt(__doc__).items()}
    merge_workspaces(**args)


if __name__ == "__main__":
    main()
