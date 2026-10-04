# Recovery field report: Deco X60, Synology DS920+, and SSH-key-only access

Thanks for documenting the Curb recovery method. I recovered my 18-channel monitor using the approach described in [curbed](https://github.com/codearranger/curbed), with a custom SSH-key-only payload. It now sends approximately one-second samples to a local receiver on my Synology NAS.

This report focuses on three potential contributions: distinguishing payload delivery from successful installation, documenting an optional public-key-only recovery path, and sharing the practical details of a Deco/Synology installation.

These observations come from one device recovered in October 2026. They are not a claim of compatibility with every Curb hardware or firmware revision. The exact hardware model and firmware version have not yet been independently verified for this report.

## My configuration

| Component | Setup |
|---|---|
| Energy monitor | 18-channel Curb hub using `/data/hub-config.json` |
| Router | TP-Link Deco X60 |
| Temporary recovery host | Mac running a local DNS forwarder and Python HTTP/HTTPS recovery server |
| Permanent receiver | Synology DiskStation DS920+, running a custom Python receiver in Docker |
| Home Assistant | Not used in this installation |
| Recovery authentication | Dedicated SSH public key; existing device passwords preserved |

During recovery, I temporarily pointed the Deco **DHCP Primary DNS** setting at the Mac. The DNS forwarder resolved `updates.energycurb.com` to the recovery host and forwarded other DNS queries to the normal resolver. The recovery service listened on DNS TCP/UDP port 53 and HTTP/HTTPS ports 80/443 on the LAN.

The permanent receiver is separate from the temporary recovery server. After SSH access was verified, the hub's endpoints were changed to the NAS receiver while preserving its existing sensor calibration.

## 1. Payload delivery is not installation success

### DNS changes did not trigger an immediate update

Changing DNS did not immediately make the hub request the recovery payload. We had to wait for its next scheduled update check. There were waits on the order of an hour, and some observed intervals between requests exceeded an hour.

The companion [ha-curb-update-server documentation](https://github.com/pvanbaren/ha-curb-update-server#how-it-works) describes an hourly update check with an additional random delay of up to 30 minutes. Therefore, “up to one hour” should not be interpreted as a guaranteed upper limit. DNS adoption and the device's update schedule are also separate steps: a successful DNS test from another computer does not establish that the hub has started using the new resolver.

### Downloads and a reboot were not enough to verify our changes

The recovery server repeatedly logged downloads of `update.tar.gz.gpg`, but authentication with our dedicated SSH key still failed. At one point, we also observed a reboot followed by checksum-only requests. Neither observation established that our intended key installation had succeeded.

We subsequently added lightweight execution-stage callbacks to the custom payload. The eventual successful attempt reported this sequence:

```text
started
backup
config_check
streamer_backup
remount_rw
key_write
key_written
exit_0_key_written
```

We then independently verified SSH public-key authentication. The distinction matters: a `key_written` callback confirms progress through the script, but a successful SSH login is the actual test of access.

### Why did SSH fail earlier?

**We did not establish a single definitive root cause.** During troubleshooting:

- We corrected the software checksum-file format to a hash-only line. This change alone did not resolve the authentication failure.
- We rebuilt our custom encrypted archive using the reference payload's GPG parameters. Our earlier generated archive used different S2K digest and iteration settings. The rebuilt archive was locally decrypted and compared with its input archive.
- Authentication was still denied after an intermediate attempt, so we added execution-stage diagnostics to distinguish delivery from execution and key installation.

Without those diagnostics on the earlier attempts, we could not determine whether each attempt failed before script execution or while applying the changes. Local decryption also did not prove successful execution on the hub. The final combination worked, but we cannot attribute success to one particular change.

These were observations from our **modified payload**, not a demonstrated defect in the project's original payload.

### Suggested documentation or status improvement

It would help to distinguish three states explicitly:

1. **Payload served:** the recovery host handled the download request.
2. **Payload executed:** the device reported execution stages and completion.
3. **SSH access verified:** authentication with the intended credentials succeeded.

Missing callbacks should remain an ambiguous result: the script may not have run, or the callback receiver may have been unreachable. They should not automatically be reported as installation failure.

## 2. An optional SSH-public-key-only recovery path

Our custom payload added a dedicated SSH public key instead of changing the existing device passwords. It:

- Backed up the original `hub-config.json` and streamer script before making changes.
- Preserved existing passwords, network endpoints, calibration, and the update scheduler during this initial recovery stage.
- Temporarily remounted the root filesystem read-write to install the public key.
- Created the SSH directory and authorized-keys file with restrictive permissions, preserving existing entries and avoiding a duplicate key entry.
- Used exit handling to attempt to restore the filesystem to read-only and report the final stage.

The private key stayed on the workstation and was never included in the payload. After verifying SSH access, we copied the configuration backup off the hub before changing its endpoints.

The project's [SSH notes](https://github.com/codearranger/curbed#ssh-notes) also describe legacy `ssh-rsa` compatibility requirements. These should be checked separately from payload execution; an SSH negotiation problem and a rejected key are different failure modes. Any compatibility exceptions should be scoped to the specific hub rather than applied globally.

This key-only approach worked on our hub. An optional documented variant could be useful for owners who prefer to preserve existing passwords. It should be presented as a tested alternative on this device, not as universally compatible without further testing.

## 3. Deco and Synology deployment experience

### Temporary DNS, then direct local collection

Using a temporary DNS forwarder through the Deco DHCP DNS setting worked in our installation. We kept the recovery host available while that setting was in use.

After SSH access was verified, we changed the hub's endpoints to the NAS receiver, incremented the configuration revision, and restarted the streamer. The original sensor calibration and other unrelated configuration fields were preserved.

We then restored the Deco DNS setting and verified that fresh approximately one-second samples continued arriving at the NAS. This demonstrated that ongoing collection no longer depended on the temporary DNS override.

The cleanup order matters: restore the router's DNS configuration before shutting down the temporary DNS service. Other clients using that service must also be able to return to their normal resolver.

### Docker source addresses affected our receiver's IP filter

Once the hub was configured to send samples to the NAS, our receiver initially rejected legitimate requests with HTTP 403. In our Synology Docker networking configuration, the receiver saw the Docker bridge address rather than the hub's original LAN address.

We adjusted the receiver to validate the hub's existing authentication information as well as the expected network source. A bridge address alone is not a device identity. We verified both that real hub samples were accepted and that unauthenticated POST requests were rejected.

This was a separate receiver-side problem after recovery. It was not evidence that the SSH recovery had failed. The source-address behavior is specific to our networking configuration and should be checked rather than assumed for all Synology or Docker installations.

## What I would do differently from the beginning

1. **Prepare an observable recovery payload.** Include backups, public-key installation, stage reporting, and exit handling from the first attempt.
2. **Validate packaging before serving it.** Check the archive contents, checksum-file format, and compatibility with the reference encryption format. Keep the original artifacts for comparison and rollback.
3. **Verify the network path, then allow for the update schedule.** Confirm the DNS override and LAN server reachability. Keep the host awake and available through a complete scheduled update interval rather than repeatedly changing the configuration after a short wait.
4. **Use separate success criteria.** Treat download logs as delivery evidence only. Verify script completion and actual SSH authentication independently. If downloads recur but access still fails, investigate execution stages and the specific SSH error instead of assuming that more waiting will resolve it.
5. **Separate recovery from receiver deployment.** Verify SSH, copy backups off the device, then change endpoints. If samples do not arrive, inspect the receiver's HTTP responses and source-address assumptions.
6. **Verify independence before finishing.** Restore temporary DNS settings, confirm that fresh samples still arrive, and then stop the temporary services.

Would you be interested in a documentation PR covering this setup, recovery diagnostics, and an optional SSH-key-only approach? I can prepare sanitized diagnostic examples and a generalized version of the key-only script for review.
