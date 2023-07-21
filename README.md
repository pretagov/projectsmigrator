# Projects Migrator

Migrates/Syncs one or more ZenHub workspaces to a Github Project

Usage:

```
python projectsmigrator.py https://github.com/orgs/myorg/myproj/1 --workspace="Workspace 1" --workspace="Workspace 2"
```

# Details

The project must be a ProjectV2 and must already exist.
- Create the Status you want

To see all the options look at https://raw.githubusercontent.com/pretagov/projectsmigrator/main/projectsmigrator.py
or run 

```
python projectsmigrator.py --help
```

## Authentication

You will need access to both the Zenhub graphql api and Github graphql api.
Once you have got auth tokens for both you can either 
- put them environment variables ```ZENHUB_TOKEN``` and ```GITHUB_TOKEN```
- use ```--zenhub-token=<token>``` and ```--github-token=<token>``` command line options

- TODO: Required token permissions

## Columns/Status
The columns/status won't be added automatically but instead
the issues will be placed in the closest existing column that matches.

## Epics
Epics are recreated as checklists in the form

``` markdown
# Dependencies

## Epic
- [ ] #23
- [ ] otherorg/otherepo#42
```

## Blocking/Blocked

An issue that is blocked will have a dependencies section added to it that lists the blocking issues

e.g.
``` markdown
# Dependencies

## Blocked by
- [ ] #23
- [ ] otherorg/otherepo#42
```

Issues that are blocking will remain unchaged.

## Linked Pull Request

Since zenhub has its own way to linking pull requests to issues this information is transfered by
modifying the PR to add text which then will use githubs automatic PR linking

e.g.
```
fixes otherorg/otherepo#42
```

it won't do this for PR's in repos outside the org your project is in

NOTE: Github linked PR's will automatically close the linked ticket once the PR is merged
to the main branch. This is different to zenhub. While you can't change this behavior, github
also doesn't move an item in your project automatically when it closes so this shouldn't change
your workflow too much.

## Workspace

You can specify which workspaces to merge using 
```
--workspace="Workspace 1" --workspace="Workspace 2"
```

They are merged in the priorty listed. ie if fields/pipeline data differs, the first workspace data will be used

If no workspaces are specified then all workspaces will be merged in most recently used order

You can optionally add the workspace name the issue came from into a new custom field, but ff not enabled this information is not transfered

```
--workspace_field="Workspace"
```


## High Priority
By default Projects has a custom field called Priority with more options than zenhubs high priority flag.
If this field exists we will migrate High Priority issues to the cloest word match.

## Estimate
The default Projects equivilent is "Size" which has a different scale to the default Zenhub story points.
This is mapped by rank. The 1st estimate option becomes the 1st size option. The last estimate option becomes the last
size option, and the rest are mapped proportionally.

NOTE: currently we haven't worked out how to read the options for estimate from zenhub so it assumes the default storypoints.
if the value doesn't match one of those then it will pick the closest story point.

# TODO

- [ ] Sprints - as a text field?
- [ ] Milestones - depricated and not possible to get from the graphql
- [ ] Release Reports?
- [ ] Handle different estimate scales
- [ ] Migrate projects -> project
- [ ] Fix inability to add more options to the workspace field
- [ ] Handle zenhub only epics and issues
- [ ] Optionally support Epics/blocking as a fields instead of checklists


