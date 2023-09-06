# Projects Migrator

Migrates one or more ZenHub workspaces to a Github Project

Install

```
pip install projectsmigrator
```

Usage:

```
projectsmigrator https://github.com/orgs/myorg/myproj/1 -w="Workspace 1" --w="Workspace 2" -f="Estimate:Size" 
```


# Details

```
Projects Migrator: Sync Zenhub workspaces into a single Github Project

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
  -h, --help                           Show this screen.

For zenhub the following fields are available.
- Estimate, Priority, Pipeline, Linked Issues, Epic, Blocking, Sprint, Position, Workspace

For Projects the fields are customisable. However the following are special
- Status: the column on the board
- Position: Id of the item to place after
- Text: turns the value into a checklist/list in the body
- Linked Pull Requests: changes to the body of each PR to link back to the Issue  
```
or [Latest Usage](https://raw.githubusercontent.com/pretagov/projectsmigrator/main/projectsmigrator.py) or run 

```
projectsmigrator --help
```


Note if you want to install a github checkout of the latest code:
```
python3 -m pip install -e .
```

## Project

The project must be a organisation ProjectV2 that already exists.

You should add the status and other field options you want manually first.


## Columns/Status
The default setting is ```-f="Pipeline:Status"```

The columns/status value won't be added automatically if it doesn't exist but instead
the issues will be placed in the closest existing column that matches.

The position with in that column is set using the default ```-f="Position:Position"```.

You can drop pipelines via ```--exclude=Pipeline:MyIgnoredPipelines*```

You can change the closest option match behaviour to an exact match via ```-f="Pipeline:Status:Exact"```

## Epics

The default setting is ```-f="Epic:Text"```

This will recreate Epics as checklists in the form

``` markdown
# Dependencies

## Epic
- [ ] #23
- [ ] otherorg/otherepo#42
```

While Projects doesn't have native support for Epics, this does seem to be what GitHub is leaning towards for the recommended way to
relate tickets togeather. When you view an issue that is a checklist elsewhere you will see this highlighted below the header and it
will include the headings "Dependencies Epic" so you can see why they are related.

One thing you lose is the ability to filter a board by Epic. If you want you can instead convert Epics as a field on the sub issue.

```-f="Epic:MyEpicField"```. This will set the value to the name of the epic.

Or you can both at the same time.

```-f="Epic:Text" -f="Epic:MyEpicField"```

GitHub doesn't currently support multivalued fields so there isn't another way to set links on the Epic issue itself via a field.

## Blocking/Blocked

The default setting is ```-f="Blocking:Text"```

An issue that is blocked will have a dependencies section added to it that lists the blocking issues

e.g.
``` markdown
# Dependencies

## Blocked by
- [ ] #23
- [ ] otherorg/otherepo#42
```

Issues that are blocking will remain unchaged.

## Linked Issues

The default setting is ```-f="Linked Issues:Text"```

Since zenhub has its own way to linking pull requests to issues this information is transfered by
modifying the PR to add text which then will use githubs automatic PR linking

e.g.
```
- [ ] fixes otherorg/otherepo#42
```

it won't do this for PR's in repos outside the org your project is in.

NOTE: Github linked PR's will automatically close the linked ticket once the PR is merged
to the main branch. This is different to zenhub. While you can't change this behavior, github
also doesn't move an item in your project automatically when it closes so this shouldn't change
your workflow too much.

## Workspace

You can specify which workspaces to merge using 
```
--workspace="Workspace 1" --workspace="Workspace 2"
```

or merge all workspaces excluding some
```
--exclude="Workspace:Workspace 1*"
```

They are merged in the priorty listed. ie if fields/pipeline data differs, the first workspace data will be used

If no workspaces are specified then all workspaces will be merged in most recently used order

You can optionally add the workspace name the issue came from into a new custom field, but ff not enabled this information is not transfered. This can be enabled with ```-f="Workspace:MyWorkspaceField"```


## Priority

The default setting is ```-f="Priority:Priority"```

By default Projects has a custom field called Priority with more options than ZenHub's "High Priority" flag.
If this field exists we will migrate High Priority issues to the cloest word match.

## Estimate

The default setting is ```-f="Estimate:Size:Scale"```

The default Projects equivilent is "Size" which has a different scale to the default Zenhub story points.

This is mapped by rank (The ```:Scale``` suffix) rather than my word match. The 1st estimate option becomes the 1st size option. The last estimate option becomes the last
size option, and the rest are mapped proportionally. Currently there doesn't seem to be an api to read the options for Estimate from zenhub so it assumes the default storypoints.
if the value doesn't match one of those then it will pick the closest story point.

If you would like to match by closest then use ```-f="Estimate:Size:Closest"```. Closest is based on letters, not numerically.
## Sprints

You can enable transfer of sprint information with ```-f="Sprint:MySprintField"```.

If the field is a singleselect field it will pick the closest matching option.

## Authentication

You will need access to both the Zenhub graphql api and Github graphql api.

Once you have got auth tokens for both you can either 
- put them environment variables ```ZENHUB_TOKEN``` and ```GITHUB_TOKEN```
- use ```--zenhub-token=<token>``` and ```--github-token=<token>``` command line options

For Github currently only Classic tokens appear to work. For this you will need ```repo```, ```admin:org``` and ```project``` permissions.
Fine grain personal access tokens don't currently appear to work.
  
# TODO

- [ ] Dry run mode
- [ ] verbose or less verbose mode. esp to help see pipline mapping more clearly
- [ ] CSV import
- [ ] Migrate projects -> project
- [ ] filtering issues by src field value
- [ ] blacklist certain field values
- [ ] include labels as a field target e.g. put workspace name as new label
- [ ] handle sync data from closed issues
- [ ] Handle zenhub only epics and issues
- [ ] zenhub Milestones - depricated and not possible to get from the graphql
- [ ] zenhub Release Reports?
- [ ] Handle different estimate scales
- [ ] Fix inability to add more options to the workspace field or removing adding fields.
- [ ] set a global default value. e.g. normal priority

# Contributing

PR's or tickets welcome

# Credits

Sponsored by PretaGov UK/AU https://pretagov.com

Inspired by [this manual process for migrating zenhub to projects](https://medium.com/collaborne-engineering/migrate-from-zenhub-to-github-projects-948d69adc17d)
but we were motiviated by
- the need for longer transition so needed some way to sync so we needed something more automated
- the need to merge many workspaces into a single project
- the idea that epics represented by checklists rather than fields would work better
- the suprise that no one had written a tool for this already and that others might find it useful.

