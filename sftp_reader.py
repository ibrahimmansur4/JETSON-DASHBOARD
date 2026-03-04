# =============================================================
#  sftp_reader.py  —  Read-only SSH/SFTP data access
#
#  The Jetson is never sent any commands.  Data is fetched by
#  byte-range reads over SFTP — identical to how FileZilla or
#  WinSCP downloads a file.  Password authentication only;
#  SSH key auth has been removed for simplicity.
# =============================================================

import io
import paramiko
import pandas as pd


def connect(host, port, username, password):
    """Open an SSH connection using password authentication."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password,
        timeout=10,
    )
    return client


def open_sftp(ssh_client, cached_sftp):
    """Return the cached SFTP session, or open a new one if it was closed."""
    if cached_sftp is None:
        return ssh_client.open_sftp()
    return cached_sftp


def file_size(sftp, remote_path):
    """Return file size in bytes, or 0 on any error."""
    try:
        return sftp.stat(remote_path).st_size
    except Exception:
        return 0


def _read_bytes(sftp, remote_path, offset=0):
    """Read all bytes from offset to end of file."""
    with sftp.open(remote_path, "rb") as f:
        f.seek(offset)
        return f.read()


def fetch_initial(sftp, remote_path, max_rows):
    """
    Download the full file and keep the last max_rows rows.

    Returns (DataFrame, byte_offset_of_first_kept_row, error_str | None).
    The byte offset is stored in session_state so incremental fetches
    know where to resume.
    """
    try:
        raw  = _read_bytes(sftp, remote_path)
        text = raw.decode("utf-8", errors="replace")
        df   = pd.read_csv(io.StringIO(text), on_bad_lines="skip", dtype=str)

        if max_rows and len(df) > max_rows:
            df = df.iloc[-max_rows:]

        # Calculate the byte position of the first row we kept,
        # so we can resume incremental reads from that point.
        all_lines   = text.splitlines(keepends=True)
        n_skipped   = max(0, text.count("\n") - max_rows) if max_rows else 0
        byte_offset = sum(len(ln.encode("utf-8")) for ln in all_lines[:n_skipped])
        return df, byte_offset, None

    except Exception as e:
        return pd.DataFrame(), 0, str(e)


def fetch_incremental(sftp, remote_path, byte_offset):
    """
    Read only the bytes that have been appended since the last fetch.

    Returns (DataFrame | None, new_byte_offset, error_str | None).
    Returns an empty DataFrame (not None) when there are no new rows yet.
    """
    try:
        size = file_size(sftp, remote_path)
        if size <= byte_offset:
            return pd.DataFrame(), byte_offset, None

        raw  = _read_bytes(sftp, remote_path, offset=byte_offset)
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return pd.DataFrame(), byte_offset, None

        df = pd.read_csv(io.StringIO(text), header=None, on_bad_lines="skip")
        return df, byte_offset + len(raw), None

    except Exception as e:
        return None, byte_offset, str(e)
