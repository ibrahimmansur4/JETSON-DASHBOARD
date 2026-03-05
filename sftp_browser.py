# =============================================================
#  sftp_browser.py  —  Visual SFTP file browser
#
#  Renders a directory browser in the Streamlit sidebar that
#  lets the user navigate the Jetson's filesystem and select
#  a CSV file instead of typing the path manually.
# =============================================================

import stat
import posixpath

import streamlit as st


def render_browser(sftp, current_path):
    """
    Render an SFTP file browser in the sidebar.

    Args:
        sftp: An open paramiko SFTPClient.
        current_path: The currently selected remote CSV path.

    Returns:
        The selected file path (str) if the user picks a file,
        or None if no selection was made this cycle.
    """
    if sftp is None:
        return None

    # Session state keys for browser state (per-session, not shared)
    if "sftp_browser_cwd" not in st.session_state:
        # Start in the directory of the current CSV path
        st.session_state.sftp_browser_cwd = posixpath.dirname(current_path) or "/"
    if "sftp_browser_open" not in st.session_state:
        st.session_state.sftp_browser_open = False

    # Toggle button
    if st.button(
        "Close Browser" if st.session_state.sftp_browser_open else "Browse Remote Files",
        use_container_width=True,
        key="sftp_browser_toggle",
    ):
        st.session_state.sftp_browser_open = not st.session_state.sftp_browser_open
        st.rerun()

    if not st.session_state.sftp_browser_open:
        return None

    cwd = st.session_state.sftp_browser_cwd
    selected = None

    # Breadcrumb navigation
    parts = cwd.strip("/").split("/") if cwd != "/" else []
    breadcrumb = "/ "
    crumb_cols = st.columns(max(len(parts) + 1, 1))
    with crumb_cols[0]:
        if st.button("/", key="sftp_crumb_root"):
            st.session_state.sftp_browser_cwd = "/"
            st.rerun()
    for idx, part in enumerate(parts):
        col_idx = min(idx + 1, len(crumb_cols) - 1)
        with crumb_cols[col_idx]:
            path_so_far = "/" + "/".join(parts[:idx + 1])
            if st.button(f"{part}/", key=f"sftp_crumb_{idx}"):
                st.session_state.sftp_browser_cwd = path_so_far
                st.rerun()

    # List directory contents
    try:
        entries = sftp.listdir_attr(cwd)
    except PermissionError:
        st.warning(f"Permission denied: {cwd}")
        return None
    except Exception as e:
        st.warning(f"Cannot list {cwd}: {e}")
        return None

    # Separate dirs and files, sort alphabetically
    dirs = []
    files = []
    for entry in entries:
        if stat.S_ISDIR(entry.st_mode):
            dirs.append(entry.filename)
        else:
            files.append(entry.filename)
    dirs.sort()
    files.sort()

    # Parent directory
    if cwd != "/":
        if st.button(".. (parent directory)", key="sftp_parent", use_container_width=True):
            st.session_state.sftp_browser_cwd = posixpath.dirname(cwd) or "/"
            st.rerun()

    # Directories
    for dirname in dirs[:50]:  # cap at 50 to avoid UI overload
        if st.button(f"[DIR]  {dirname}/", key=f"sftp_dir_{dirname}", use_container_width=True):
            st.session_state.sftp_browser_cwd = posixpath.join(cwd, dirname)
            st.rerun()

    # Files — highlight CSVs
    csv_files = [f for f in files if f.lower().endswith(".csv")]
    other_files = [f for f in files if not f.lower().endswith(".csv")]

    for fname in csv_files[:50]:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"**[CSV]  {fname}**")
        with c2:
            if st.button("Select", key=f"sftp_sel_{fname}"):
                selected = posixpath.join(cwd, fname)
                st.session_state.sftp_browser_open = False

    for fname in other_files[:50]:
        st.caption(f"[---]  {fname}")

    if len(dirs) > 50 or len(files) > 50:
        st.caption(f"Showing first 50 entries. {len(dirs)} dirs, {len(files)} files total.")

    return selected
