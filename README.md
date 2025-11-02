# Dankbit — local dev Docker notes

This project runs Odoo inside Docker for local development. The compose file mounts the Odoo data directory (including the filestore) into your project so you can inspect and back it up easily.

## What changed
- The compose file mounts the Odoo data dir to `./odoo_filestore` on the host:

  ./odoo_filestore:/var/lib/odoo/.local/share/Odoo

  This makes the filestore and related Odoo data visible on your host and persistent across container restarts and rebuilds.

- At container start the `web` service runs a quick `chown` so the container user can write into the host folder.

## Important notes / warnings
- Binding a host directory will hide any content that previously lived inside the image at that path. If you previously used a named Docker volume (e.g., `odoo-filestore`), migrate its contents to `./odoo_filestore` before switching to the bind mount. See migration steps below.

- If the host folder is created empty and Odoo runs as a different UID/GID, the container may not be able to write into the folder — the compose config runs a `chown` at container start to mitigate this.

## Migration from existing named volume (if applicable)
If you used a named volume before and want to copy its contents into the host bind, follow these steps:

1. Find the actual name of the volume (compose may prefix it with project name):

```bash
docker volume ls
```

2. Inspect the volume to get its name shown as `Mountpoint`:

```bash
docker volume inspect <volume_name>
```

3. Copy files from the named volume into the new host folder using a temporary container (replace `<volume_name>`):

```bash
mkdir -p ./odoo_filestore
docker run --rm -v <volume_name>:/from -v "$(pwd)/odoo_filestore":/to alpine sh -c "cd /from && tar -c ." | tar -x -C ./odoo_filestore
```

4. Verify files landed in `./odoo_filestore`.

## Backup and restore
- Backup the filestore (run from project root):

```bash
tar -czf dankbit-filestore-$(date +%F).tar.gz odoo_filestore
```

- Restore into the host bind (stop compose first):

```bash
# stop the stack
docker compose down
# restore
tar -xzf dankbit-filestore-YYYY-MM-DD.tar.gz -C ./odoo_filestore
# start again
docker compose up -d
```

- Alternatively, restore directly into the named volume using a temporary container:

```bash
docker run --rm -i -v odoo-filestore:/data alpine sh -c "cd /data && tar xzf -" < dankbit-filestore-YYYY-MM-DD.tar.gz
```

## Start the stack

```bash
# build and start
docker compose up -d --build
```

If you'd like I can:
- Start the stack and confirm the `./odoo_filestore` folder is populated and owned correctly, and fetch a sample chart to ensure everything renders; or
- Revert to using the named volume if you prefer that over a host bind.

Let me know which you'd like.