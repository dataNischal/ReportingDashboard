"""
file_io_utils.py -- local-only atomic write helper (Report Dashboard project).

Trimmed from the original DATA MIGRATION project's version: that one had a
local/S3 destination toggle (this pipeline used to write CSVs to S3). This
project has no S3 output at all -- Codes/postgres_pipeline.py writes
straight to Postgres, and Codes/interactive_dashboard_generator.py's
generated HTML is always written to local disk -- so only atomic writing
survives here.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)


class AtomicFileWriteError(OSError):
    """Raised when a target file can't be replaced after all retries --
    typically because it's open in another program."""


class AtomicFileWriter:
    """
    Writes to `<path>.tmp` first, then os.replace()s it into place --
    atomic on both Windows and POSIX, so a crash mid-write never corrupts
    the existing file. A locked target (open in a browser/editor) is
    retried a few times before raising one clear, actionable error.
    """

    def __init__(self, max_retries: int = 3, retry_delay_seconds: float = 1.0):
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    def write(self, output_path, write_fn) -> str:
        """write_fn: callable(path) that writes the desired content to the
        given path, e.g. `lambda p: Path(p).write_text(html, encoding="utf-8")`."""
        output_path = str(output_path)
        output_dir = os.path.dirname(output_path) or "."
        os.makedirs(output_dir, exist_ok=True)
        temp_path = output_path + ".tmp"

        write_fn(temp_path)

        last_error = None
        for attempt in range(self.max_retries):
            try:
                os.replace(temp_path, output_path)
                return output_path
            except PermissionError as e:
                last_error = e
                logger.warning(
                    "Could not replace %s (attempt %d/%d): %s",
                    output_path, attempt + 1, self.max_retries, e,
                )
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay_seconds)

        try:
            os.remove(temp_path)
        except OSError:
            pass

        raise AtomicFileWriteError(
            f"Could not update '{output_path}' after {self.max_retries} attempts -- it "
            f"appears to be open in another program (e.g. a text editor, or a "
            f"browser still downloading/rendering it). Close it and re-run. "
            f"(Original error: {last_error})"
        )

    def write_text(self, content: str, output_dir, file_name: str) -> str:
        """Writes `content` as `file_name` under `output_dir`, returns the final path."""
        output_path = os.path.join(str(output_dir), file_name)

        def _write(tmp_path: str) -> None:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)

        return self.write(output_path, _write)


# Backwards-compatible functional entrypoints.
def atomic_write(output_path, write_fn, max_retries=3, retry_delay_seconds=1.0) -> str:
    return AtomicFileWriter(max_retries, retry_delay_seconds).write(output_path, write_fn)


def write_text(content, output_dir, file_name) -> str:
    return AtomicFileWriter().write_text(content, output_dir, file_name)
