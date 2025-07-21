fido-ssh
========

Automation to share a local hardware security key with a remote server.

Usage
-----

```sh
fssh <ssh options>
```

All arguments after `fssh` are passed to the `ssh` command. For example:

```sh
fssh user@remote-server
```

Note: Non-interactive mode is not supported. For example:

```sh
fssh user@remote-server "sudo apt update && sudo apt upgrade -y"
```

Will execute the user's command prior to attaching the security key to the remote, therefore negating the benefit of `fssh`.

