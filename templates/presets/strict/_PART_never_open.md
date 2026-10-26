**Subagent charters — the never-open clause.** Every subagent, adversarial reviewer or helper session you start gets the paragraph below pasted into its charter VERBATIM, and passes it on verbatim to any charter it writes in turn:

> NEVER-OPEN (binding, verbatim): never open, read, print, grep, parse, copy or summarise
> `.env*`, `*.tfvars*`, `*.tfplan*`, `terraform.tfstate*` (including
> `.terraform/terraform.tfstate`), `*.local*`, `secrets/`, `*.pem`, `*.key`,
> `id_rsa*`, `credentials*`, or any path the workspace rules mark sealed. Inventory
> them by name, size and mtime only: `find <dir> \( -name '.env*' -o -name '*.tfvars*'
> -o -name '*.tfplan*' -o -name 'terraform.tfstate*' -o -name '*.local*' \) -printf
> '%p %s %TY-%Tm-%Td\n'`. Every recursive search excludes them:
> `grep -r --exclude='.env*' --exclude='*.tfvars*' --exclude='*.tfplan*'
> --exclude='terraform.tfstate*' --exclude='*.local*' --exclude-dir='*.local*'
> --exclude-dir='.terraform' …`. If one is opened by accident, say so at once (its
> class and path, never a value) and quote nothing from it.
