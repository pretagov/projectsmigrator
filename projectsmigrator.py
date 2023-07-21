"""Projects Migrator: Sync Zenhub workspaces into a single Github Project

Usage:
  projectsmigrator.py <project_url> [--workspace=<name>]... [--exclude-workspace=<name>]... [--exclude-pipeline=<name>]... [options]
  projectsmigrator.py (-h | --help)

Options:
  -w=<name>, --workspace=<name>  Name of a Zenhub workspace or none means include all.
  --exclude-workspace=<name>     Don't merge the matching Zenhub workspace name.
  --exclude-pipeline=<name>      Exclude pipelines names that match pattern.
  --estimate-field=<name>        Github field name to put estimates in [default: Size].
  --priority-field=<name>        Github field name to put priority in [default: Priority].
  --workspace-field=<name>       Github field name to put workspace name in. Empty means skip.
  --disable-blocking-checklist   Don't edit issue to add a checklist of blocking issues.
  --disable-epic-checklist       Don't edit epic issue to add a checklist of subitems.
  --disable-pr-link              Don't edit linked PR to add "fixes #??".
  --disable-remove               Project items not found in any of the workspace won't be removed.
  --github-token=<token>         or use env var GITHUB_TOKEN.
  --zenhub-token=<token>         or use env var ZENHUB_TOKEN.
  -h, --help                     Show this screen.

"""

from docopt import docopt
import difflib
from gql import gql, Client, transport
from gql.transport.aiohttp import AIOHTTPTransport
import os
import fnmatch


def merge_workspaces(project_url, workspace, **args):
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
    fields = {
        f["name"]: f
        for f in gh_query(gh_get_Fields, dict(proj=proj["id"]))["node"]["fields"]["nodes"]
    }

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
    for i, next in zip(items[:-1], items[1:]):
        next["after"] = i
    items = {issue_key(item["content"]): item for item in items}

    workspaces = {
        ws["name"]: ws for ws in zh_query(zh_workspaces)["recentlyViewedWorkspaces"]["nodes"]
    }
    if not workspace:
        workspace = list(workspaces.keys())
    workspace = [
        w
        for w in workspace
        if not any(fnmatch.fnmatch(w, pat) for pat in args["exclude_workspace"])
    ]
    if args["workspace_field"] and args["workspace_field"] not in fields:
        # grey = gh_query.__self__.schema.type_map['ProjectV2SingleSelectFieldOptionColor'].values['GRAY']
        options = [
            dict(fuzzy_get(workspaces, name), description="", color="GRAY") for name in workspace
        ]
        field = gh_query(gh_add_field, dict(name="Workspace", proj=proj["id"], options=options))
        fields["Workspace"] = field
        # TODO: add in missing options

    seen = {}
    for name in workspace:
        ws = fuzzy_get(workspaces, name)
        sync_workspace(ws, proj, fields, items, seen, zh_query, gh_query, **args)

    # get list of all items in the current project so we can remove ones added by mistake if desired.
    if not args["disable_remove"]:
        print(f"Not in any workspace")
        all_items = set((r, i.get("content", {}).get("title"), i["id"]) for r, i in items.items())
        added_items = set((r, i.get("content", {}).get("title"), i["id"]) for r, i in seen.items())
        for repo, name, item in all_items - added_items:
            gh_query(gh_del_item, dict(proj=proj["id"], issue=item))
            print(f"- '{repo[0]}':'{name}' - REMOVED")


def sync_workspace(ws, proj, fields, items, seen, zh_query, gh_query, **args):
    epics = {
        e["issue"]["id"]: e
        for e in zh_query(zh_get_epics, dict(workspaceId=ws["id"]))["workspace"]["epics"]["nodes"]
    }
    deps = zh_query(zh_get_dep, dict(workspaceId=ws["id"]))["workspace"]["issueDependencies"][
        "nodes"
    ]

    # Get proj states
    estimate_field = fields.get(args["estimate_field"])
    priority_field = fields.get(args["priority_field"])
    workspace_field = fields.get(args["workspace_field"])
    status_field = fields["Status"]
    options = {opt["name"]: opt for opt in status_field["options"]}

    for pos, pipeline in enumerate(ws["pipelines"]):
        issues = zh_query(
            zh_issues, dict(pipelineId=pipeline["id"], filters={}, workspaceId=ws["id"])
        )["searchIssuesByPipeline"]["nodes"]

        if any(
            fnmatch.fnmatch(pipeline["name"], drop_col) for drop_col in args["exclude_pipeline"]
        ):
            print(f"Excluding Pipeline '{ws['name']}/{pipeline['name']}'")
            continue

        # add state if we don't have it. # TODO. no api for this yet. Have to create the whole field
        # for now we will just pick closest match
        status = fuzzy_get(options, pipeline["name"])
        print(f"Merging {ws['name']}/{pipeline['name']} -> {proj['title']}/{status['name']}")
        last = None
        for issue in issues:
            changes = []
            key = issue_key(issue)
            if key in items:
                item = items[key]
                gh_issue = item["content"]
            else:
                gh_issue = gh_query(
                    gh_get_issue,
                    dict(
                        number=issue["number"],
                        repo=issue["repository"]["name"],
                        owner=issue["repository"]["owner"]["login"],
                    ),
                )["repository"]["issueOrPullRequest"]
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
                items["key"] = item
                changes += ["ADD*"]
            if key in seen:
                print(f"- '{issue['repository']['name']}':'{issue['title']}' - SKIP")
                continue
            else:
                seen[key] = item

            # set column/status
            if status_field is not None:
                changes += (
                    ["COL*"] if set_field(proj, item, status_field, status["id"], gh_query) else []
                )

            # set order/position
            if last is None:
                pass
            elif item.get("after", {}).get("id") == last["id"]:
                changes += []
            else:
                try:
                    gh_query(
                        gh_set_order, dict(proj=proj["id"], item=item["id"], after=last["id"])
                    )
                except transport.exceptions.TransportQueryError as e:
                    # TODO: work out why error happened
                    changes += [f"POS({e})"]
                else:
                    changes += ["POS*"]
            last = item

            # set priority
            if issue["pipelineIssue"]["priority"] and priority_field is not None:
                priority = fuzzy_get(
                    {opt["name"]: opt for opt in priority_field["options"]},
                    issue["pipelineIssue"]["priority"]["name"],
                )
                changes += (
                    ["PRI*"]
                    if set_field(proj, item, priority_field, priority["id"], gh_query)
                    else ["PRI"]
                )

            # set estimate
            if issue["estimate"] and estimate_field is not None:
                # 8 steps
                story_points = [40, 21, 13, 8, 5, 3, 2, 1]
                # TODO: if another scale we just pick closest number. Should get scale from zenhub but how?
                pos = min(
                    range(len(story_points)),
                    key=lambda i: abs(story_points[i] - issue["estimate"]["value"]),
                )
                size = estimate_field["options"][
                    round(pos / len(story_points) * len(estimate_field["options"]))
                ]
                changes += (
                    ["EST*"]
                    if set_field(proj, item, estimate_field, size["id"], gh_query)
                    else ["EST"]
                )

            # put in original workspace name as custom field
            # - we can always get rid of it later
            # - we can automate setting it later based on repo
            # - allows us to set client on external issues
            if workspace_field is not None:
                value = next(
                    (f["id"] for f in workspace_field["options"] if f["name"] == ws["name"]), None
                )
                changes += (
                    ["WS*"] if set_field(proj, item, workspace_field, value, gh_query) else []
                )

            # TODO: hacky way to get get linked PR. but can't see another way yet
            for hist in [
                hist["data"]
                for hist in issue["timelineItems"]["nodes"]
                if "issue.connect_issue_to_pr" in hist["type"]
            ]:
                if args["disable_pr_link"]:
                    continue
                powner, prepo, pnumber = (
                    hist["pull_request_organization"]["login"],
                    hist["pull_request_repository"]["name"],
                    hist["pull_request"]["number"],
                )
                # TODO: see if already linked in field_value(item, fields['Linked pull requests'])
                value = field_value(item, fields["Linked pull requests"])
                if value is not None:
                    prurl = f"https://github.com/{powner}/{prepo}/pull/{pnumber}"
                    if any(pr["url"] == prurl for pr in value["nodes"]):
                        # Already linked
                        changes += ["PR"]
                        continue

                pr = gh_query(gh_get_pr, dict(number=pnumber, repo=prepo, owner=powner))[
                    "repository"
                ]["pullRequest"]
                if powner != proj["owner"]["login"]:
                    # Let's skip modifying others PR's
                    print(f"- SKIP PR update on '{pr['title']}'")
                    continue

                fixes = f"fixes {shorturl(gh_issue['url'])}"
                if fixes not in pr["body"]:
                    newbody = pr["body"] + f"\r\n {fixes}"
                    gh_query(gh_setpr_body, dict(id=pr["id"], body=newbody))
                    changes += ["PR*"]
                else:
                    changes += ["PR"]

            text = ""
            # TODO: there is special linked PR field but can't set it? https://github.com/orgs/community/discussions/40860
            # seems like it only lets you create a branch, not link a PR? do via discussion? or work out how UI lets you set it?
            # gh_query(gh_set_value, dict(proj=proj, item=item['id'], field=fields['Linked pull requests']['id'], value=pr['url']))

            # if epic, put in checklist
            # TODO: we don't reproduce closed epics. Possibly find tickets that are part of epics and go change the closed ones?
            if issue["id"] in epics and not args["disable_epic_checklist"]:
                text += "\n## Epic\n"
                epic = epics[issue["id"]]
                for subissue in zh_query(
                    zh_epic_issues, dict(zenhubEpicId=epic["id"], workspaceId=ws["id"])
                )["node"]["childIssues"]["nodes"]:
                    text += f"- [ ] {subissue['htmlUrl']}\n"

            # TODO: if blocking, put in checklist - in hist as 'issue.add_blocked_issue'
            blocked = {
                i["blockingIssue"]["id"]: i["blockingIssue"]
                for i in deps
                if i["blockedIssue"]["id"] == issue["id"]
            }
            if blocked and not args["disable_blocking_checklist"]:
                text = "\n## Blocked by\n"
                for sub in blocked.values():
                    text += f"- [ ] {sub['htmlUrl']}\n"
            # We won't include blocking since then we have the information in two places. More important to show what we block
            # also what is blocking us could be externa issue
            # blocking = {i['blockedIssue']['id']:i['blockedIssue'] for i in deps if i['blockingIssue']['id'] == issue['id']}
            # if blocking:
            #   text += "\n## Blocking\n"
            #   for sub in blocking.values():
            #         text += f"- [ ] {sub['htmlUrl']}\n"
            if text and gh_issue["repository"]["owner"]["login"] != proj["owner"]["login"]:
                print(f"- SKIP dep update on '{issue['title']}'")
            elif text:
                body, *rest = gh_issue["body"].split("\r\n# Dependencies\r\n", 1)
                rest = "" if not rest else rest[0]
                if "\n# " in rest:
                    rest = rest[rest.find("\r\n# ") :]
                else:
                    rest = ""
                text = ("\n# Dependencies\n" + text).replace("\n", "\r\n")
                newbody = body + text + rest
                if gh_issue["body"] != newbody:
                    gh_query(gh_set_body, dict(id=gh_issue["id"], body=newbody))
                    changes += ["DEP*"]
                else:
                    changes += ["DEP"]

            # TODO: can we reproduce the history?

            print(f"- '{issue['repository']['name']}':'{issue['title']}' - {', '.join(changes)}")


def fuzzy_get(dct, key):
    """
    return the value whose key is the closest

      >>> fuzzy_get({"foo":1, "bah":2}, "fo")
      1
    """
    closest = next(iter(difflib.get_close_matches(key, dct.keys(), 1, 0)))
    return dct[closest]


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


def set_field(proj, item, field, value, gh_query):
    if not field_value(item, field) == value:
        query = gh_set_option if "options" in field else gh_set_value
        if value is None:
            gh_query(gh_del_value, dict(proj=proj["id"], item=item["id"], field=field["id"]))
        else:
            gh_query(query, dict(proj=proj["id"], item=item["id"], field=field["id"], value=value))
        return True
    else:
        return False


def issue_key(issue):
    return (issue["repository"]["owner"]["login"], issue["repository"]["name"], issue["number"])


def shorturl(url):
    return url.replace("https://github.com/", "").replace("/issues/", "#")


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
                  body
                  repository {id name archivedAt owner{login }}
                }
                ... on PullRequest {
                  title
                  url
                  id
                  number
                  body        
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
            updatedAt
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
  mutation($proj:ID!, $item:ID!, $after:ID!) {
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
    args = {k.strip("<>-- ").replace("-", "_"): v for k, v in docopt(__doc__).items()}
    merge_workspaces(**args)


if __name__ == "__main__":
    main()
