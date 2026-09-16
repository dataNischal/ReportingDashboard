# ============================================================
# CLEANING_FUNCTIONS.PY
# YAML-driven cleaning, business report, and analytics helpers
# ============================================================

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

VALID_RAW_DATA_SOURCE_MODES = {"s3", "local"}
VALID_OUTPUT_DESTINATION_MODES = {"s3", "local"}
OUTPUT_DESTINATION_KEYS = ("Inficare", "iSendHub", "Dashboard")
VALID_TIMEZONE_MODES = {"fixed", "by_country", "skip"}


class CleaningConfigError(ValueError):
    """Raised when cleaning_config.yml fails validation (unknown mode, bad
    timezone name, missing required key for the selected mode, etc.) --
    always at load time, never deep inside a cleaning/transform call."""


class CleaningConfig:
    """
    Wraps cleaning_config.yml: loads it once, validates it eagerly (fail
    fast on a typo'd mode or bad timezone name at startup rather than deep
    inside a groupby/tz_localize call mid-pipeline), and exposes the
    sections DataCleaner/BusinessReportBuilder need as plain properties.
    """

    def __init__(self, raw: dict):
        self._raw = raw
        self._validate_timezone_config()
        self._validate_raw_data_source_config()
        self._validate_output_destination_config()

    @classmethod
    def load(cls, config_path: str) -> "CleaningConfig":
        """
        Always a local, git-tracked file read (config_path is a plain
        filesystem path, e.g. PROJECT_ROOT / "cleaning_config.yml") -- this
        never changes regardless of where raw_data_source/output_destination
        point reads/writes at, so the config ships with the application at
        deployment exactly like any other source file.
        """
        logger.info("Loading cleaning config from %s", config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return cls(raw)

    @property
    def raw(self) -> dict:
        return self._raw

    def table_config(self, table_name: str) -> dict:
        return self._raw["tables"][table_name]

    @property
    def shared_mappings(self) -> dict:
        return self._raw.get("shared_mappings", {})

    @property
    def shared_transformations(self) -> dict:
        return self._raw.get("shared_transformations", {})

    @property
    def timezone_normalization(self) -> dict:
        return self._raw.get("timezone_normalization", {})

    @property
    def business_reporting(self) -> dict:
        return self._raw.get("business_reporting", {})

    # ------------------------
    # VALIDATION
    # ------------------------

    def _validate_raw_data_source_config(self) -> None:
        """
        Fail fast on a typo'd raw_data_source.mode (or a missing
        bucket/prefix/path entry for the selected mode) at load time, rather
        than deep inside transaction_report.main()'s raw-data loading step.
        """
        src_cfg = self._raw.get("raw_data_source")
        if not src_cfg:
            return

        mode = src_cfg.get("mode")
        if mode not in VALID_RAW_DATA_SOURCE_MODES:
            raise CleaningConfigError(
                f"raw_data_source.mode: unknown mode '{mode}'. Valid modes: "
                f"{sorted(VALID_RAW_DATA_SOURCE_MODES)}"
            )

        mode_cfg = src_cfg.get(mode) or {}
        if mode == "s3":
            if not mode_cfg.get("bucket"):
                raise CleaningConfigError(
                    "raw_data_source.s3.bucket is required when mode is 's3'."
                )
            for source_name in ("Inficare", "iSendHub"):
                if not mode_cfg.get("prefixes", {}).get(source_name):
                    raise CleaningConfigError(
                        f"raw_data_source.s3.prefixes.{source_name} is required when mode is 's3'."
                    )
        elif mode == "local":
            for source_name in ("Inficare", "iSendHub"):
                if not mode_cfg.get(source_name):
                    raise CleaningConfigError(
                        f"raw_data_source.local.{source_name} is required when mode is 'local'."
                    )

    def _validate_output_destination_config(self) -> None:
        """
        Fail fast on a typo'd output_destination.mode (or a missing
        bucket/prefix/path entry for the selected mode) at load time, rather
        than deep inside a write step. Mirrors _validate_raw_data_source_config.
        """
        dest_cfg = self._raw.get("output_destination")
        if not dest_cfg:
            return

        mode = dest_cfg.get("mode")
        if mode not in VALID_OUTPUT_DESTINATION_MODES:
            raise CleaningConfigError(
                f"output_destination.mode: unknown mode '{mode}'. Valid modes: "
                f"{sorted(VALID_OUTPUT_DESTINATION_MODES)}"
            )

        mode_cfg = dest_cfg.get(mode) or {}
        if mode == "s3":
            if not mode_cfg.get("bucket"):
                raise CleaningConfigError(
                    "output_destination.s3.bucket is required when mode is 's3'."
                )
            for output_key in OUTPUT_DESTINATION_KEYS:
                if not mode_cfg.get("prefixes", {}).get(output_key):
                    raise CleaningConfigError(
                        f"output_destination.s3.prefixes.{output_key} is required "
                        f"when mode is 's3'."
                    )
        elif mode == "local":
            for output_key in OUTPUT_DESTINATION_KEYS:
                if not mode_cfg.get(output_key):
                    raise CleaningConfigError(
                        f"output_destination.local.{output_key} is required when mode is 'local'."
                    )

    def _validate_timezone_config(self) -> None:
        """
        Fail fast on a typo'd timezone name or an unknown `mode` at load
        time, rather than deep inside a groupby/tz_localize call during
        DataCleaner.normalize_timezones().
        """
        tz_cfg = self._raw.get("timezone_normalization")
        if not tz_cfg:
            return

        zone_names = {tz_cfg["target_timezone"]}
        zone_names.update(tz_cfg.get("country_timezones", {}).values())

        for table_name, table_rules in tz_cfg.get("tables", {}).items():
            for column, rule in table_rules.items():
                mode = rule.get("mode")
                if mode not in VALID_TIMEZONE_MODES:
                    raise CleaningConfigError(
                        f"timezone_normalization.tables.{table_name}.{column}: "
                        f"unknown mode '{mode}'. Valid modes: {sorted(VALID_TIMEZONE_MODES)}"
                    )
                if mode == "fixed":
                    zone_names.add(rule["timezone"])

        for name in zone_names:
            try:
                ZoneInfo(name)
            except ZoneInfoNotFoundError as exc:
                raise CleaningConfigError(
                    f"timezone_normalization: unknown IANA zone '{name}'"
                ) from exc


# Backwards-compatible functional entrypoint -- some callers (or ad-hoc
# scripts) may still `from cleaning_functions import load_cleaning_config`.
def load_cleaning_config(config_path: str) -> dict:
    return CleaningConfig.load(config_path).raw


class DataCleaner:
    """
    YAML-driven cleaning: applies cleaning_config.yml's transformations/
    mapping_ref/defaults rules to a raw DataFrame (clean()), and converts
    declared date columns to one consistent reporting timezone
    (normalize_timezones()). Config-driven throughout -- no column names or
    mapping tables are hardcoded here; adding a new raw column mapping/
    default means editing cleaning_config.yml, not this class.
    """

    def __init__(self, config: CleaningConfig):
        self.config = config

    # ------------------------
    # TIMESTAMP TRANSFORMATION
    # ------------------------

    @staticmethod
    def parse_timestamp(series: pd.Series, formats: list) -> pd.Series:
        """
        Parse timestamps using multiple formats from YAML.
        Returns YYYY-MM-DD HH:MM:SS format.
        """
        result = pd.Series(pd.NaT, index=series.index)
        result.name = series.name  # preserve column name so the log line below is useful

        logger.info("Parsing timestamps with formats: %s", formats)

        for fmt in formats:
            mask = result.isna()
            if mask.any():
                result.loc[mask] = pd.to_datetime(series.loc[mask], format=fmt, errors="coerce")

        # Final fallback for mixed / unrecognized formats.
        #
        # BUG FIX: pd.to_datetime() without an explicit format infers ONE
        # format from a sample of the array and applies it to every element --
        # it does not try each value independently. When this mask's leftover
        # rows are themselves a mix of sub-formats (e.g. date-only "M/D/YYYY"
        # from one month's export alongside date+time "M/D/YYYY H:MM" from
        # another), whichever sub-format pandas infers from an early sample
        # "wins": every row matching it parses fine, every row that doesn't
        # silently becomes NaT -- even though each value is individually a
        # perfectly valid, parseable date. This is exactly what happened to
        # 2024's Mar/Apr/May/Jun/Aug data once combined with Jan/Feb's export
        # (confirmed via a minimal repro: a small mixed-order array reproduces
        # the same one-sub-format-wins behavior; format="mixed" fixes it).
        # format="mixed" makes pandas infer a format PER ELEMENT instead of
        # once for the whole array, so every sub-format present succeeds.
        mask = result.isna()
        if mask.any():
            result.loc[mask] = pd.to_datetime(
                series.loc[mask], errors="coerce", format="mixed",
            )

        logger.info(
            "%s - Parsed timestamps with final format: %%Y-%%m-%%d %%H:%%M:%%S", result.name,
        )
        return result.dt.strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------
    # APPLY MAPPING
    # ------------------------

    @staticmethod
    def apply_mapping(series: pd.Series, mapping: dict) -> pd.Series:
        """
        Case/whitespace-insensitive mapping lookup. Raw source data has the
        same logical value spelled multiple ways (e.g. "Hong kong" vs
        "HONG KONG"); matching case-insensitively lets more of these hit the
        mapping dict without changing what a match gets replaced WITH (the
        dict's values are returned as authored).
        """
        if not mapping:
            return series

        normalized_map = {str(k).strip().casefold(): v for k, v in mapping.items()}
        keys = series.astype("string").str.strip().str.casefold()
        return keys.map(normalized_map).fillna(series)

    # ------------------------
    # COUNTRY TEXT NORMALIZATION
    # ------------------------

    @staticmethod
    def normalize_country_text(series: pd.Series) -> pd.Series:
        """
        Canonicalize free-text country names: strip, collapse internal
        whitespace, uppercase. Applied AFTER mapping_ref/override_from so it
        catches both values that matched the mapping dict and values that
        never had a dict entry but were already a correctly-spelled country
        name in inconsistent casing (e.g. "Australia" vs "AUSTRALIA") --
        both converge on one final string.

        Scoped to columns that opt in via `normalize_text: true` in
        cleaning_config.yml (see clean()), not applied globally by
        apply_mapping(), since other mapped fields (e.g. payment_type) have
        intentionally mixed-case target values.
        """
        cleaned = series.astype("string").str.strip()
        cleaned = cleaned.str.replace(r"\s+", " ", regex=True)
        return cleaned.str.upper()

    # ------------------------
    # CLEANING RULES
    # ------------------------

    def clean(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        table_cfg = self.config.table_config(table_name)

        # ----------------------------------
        # DIAGNOSTIC CHECKS
        # ----------------------------------
        defaults = table_cfg.get("defaults", {})
        missing_default_columns = [col for col in defaults if col not in df.columns]
        if missing_default_columns:
            logger.warning(
                "[%s] Config default columns not found: %s", table_name, missing_default_columns,
            )

        # ----------------------------------
        # NORMALIZE BLANKS TO NULL
        # ----------------------------------
        df = df.replace(["", " ", "NULL", "null", "NaN", "nan"], pd.NA)

        # ----------------------------------
        # DATA TRANSFORMATIONS
        # ----------------------------------
        transformations = table_cfg.get("transformations", {})

        for target_col, rule in transformations.items():
            source_col = rule.get("source_column", target_col)
            if source_col not in df.columns:
                continue

            # DATE TRANSFORMATION
            if rule.get("transform_ref") == "postgres_timestamp":
                formats = self.config.shared_transformations["postgres_timestamp"]["input_formats"]
                df[source_col] = self.parse_timestamp(df[source_col], formats)
                if source_col != target_col:
                    df.rename(columns={source_col: target_col}, inplace=True)

        # ----------------------------------
        # MAPPING TRANSFORMATIONS
        # ----------------------------------
        shared_maps = self.config.shared_mappings

        for col in df.columns:
            if col not in transformations:
                continue
            rule = transformations[col]

            # MAPPING_REF
            if "mapping_ref" in rule:
                mapping_dict = shared_maps.get(rule["mapping_ref"], {})
                df[col] = self.apply_mapping(df[col], mapping_dict)

            # OVERRIDE_FROM
            if "override_from" in rule:
                override_rule = rule["override_from"]
                override_col = override_rule["source_column"]
                override_map = shared_maps.get(override_rule["mapping_ref"], {})

                if override_col in df.columns:
                    override_values = df[override_col].map(override_map)
                    df[col] = override_values.fillna(df[col])

            # TEXT NORMALIZATION (case/whitespace canonicalization) -- must
            # run after mapping_ref/override_from so it also canonicalizes
            # values those steps couldn't map (no dict entry), converging
            # every casing variant of the same country on one final string.
            if rule.get("normalize_text"):
                df[col] = self.normalize_country_text(df[col])

        # ----------------------------------
        # REPLACE EMPTY STRINGS CREATED BY
        # TIMESTAMP CONVERSION
        # ----------------------------------
        df = df.replace(r"^\s*$", pd.NA, regex=True)

        # ----------------------------------
        # APPLY DEFAULTS LAST
        # ----------------------------------
        applicable_defaults = {col: value for col, value in defaults.items() if col in df.columns}
        df = df.fillna(applicable_defaults)

        # ----------------------------------
        # FINAL NULL COUNT REPORT
        # ----------------------------------
        remaining_nulls = (
            df[list(applicable_defaults.keys())].isna().sum()
            if applicable_defaults else pd.Series(dtype=int)
        )
        nonzero_nulls = remaining_nulls[remaining_nulls > 0]
        if not nonzero_nulls.empty:
            logger.info("[%s] Null counts after cleaning:\n%s", table_name, nonzero_nulls)

        return df

    # ------------------------
    # TIMEZONE NORMALIZATION
    # ------------------------

    @staticmethod
    def _convert_series_tz(naive_series: pd.Series, source_tz: str, target_tz: str) -> pd.Series:
        """
        Localize a naive datetime series to source_tz, convert to target_tz,
        and return it naive again (formatted the same way parse_timestamp
        already formats dates, so the string contract downstream -- analytics
        date parsing, the duration_hours formula -- is unchanged).

        NaT inputs pass straight through tz_localize/tz_convert as NaT, and
        NaT.strftime(...) yields NaN -- a null Paid_Date stays null with no
        extra guard code needed.
        """
        localized = naive_series.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="NaT")
        converted = localized.dt.tz_convert(target_tz).dt.tz_localize(None)
        return converted.dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _convert_series_by_group(
        naive_series: pd.Series, tz_name_series: pd.Series, target_tz: str,
    ) -> pd.Series:
        """
        Row-level timezone conversion: tz_name_series gives each row's own
        source timezone (e.g. looked up from that row's Receiver_Country).
        Rows whose tz_name is NaN (unresolved country) are left at the
        pre-initialized NaT -- i.e. that row's date becomes null rather than
        guessing a timezone, per the confirmed design decision. groupby()
        drops NaN-key groups by default, so those rows are simply never
        written into `out`, which starts as all-NaT.
        """
        out = pd.Series(pd.NaT, index=naive_series.index, dtype="datetime64[ns]")
        for tz_name, idx in tz_name_series.groupby(tz_name_series).groups.items():
            localized = naive_series.loc[idx].dt.tz_localize(
                tz_name, ambiguous="NaT", nonexistent="NaT",
            )
            out.loc[idx] = localized.dt.tz_convert(target_tz).dt.tz_localize(None)
        return out.dt.strftime("%Y-%m-%d %H:%M:%S")

    def normalize_timezones(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """
        Convert every date column declared in cleaning_config.yml's
        timezone_normalization section (already parsed into naive local-time
        strings by clean()) into one consistent clock (target_timezone --
        the preferred reporting timezone, e.g. Malaysia) so Turn-Around-Time
        and monthly-trend bucketing compare apples-to-apples across sources.
        Columns declared `mode: skip` are left completely untouched (they are
        already in target_timezone, e.g. Inficare's TRN_Date). Returns a new
        dataframe; does not mutate df.
        """
        tz_cfg = self.config.timezone_normalization
        if not tz_cfg:
            return df

        target_tz = tz_cfg["target_timezone"]
        country_tz_map = tz_cfg.get("country_timezones", {})
        table_rules = tz_cfg.get("tables", {}).get(table_name, {})

        df = df.copy()

        for column, rule in table_rules.items():
            if column not in df.columns:
                logger.warning(
                    "[%s] timezone_normalization column '%s' not found. Skipping.",
                    table_name, column,
                )
                continue

            if rule.get("mode") == "skip":
                continue

            naive = pd.to_datetime(df[column], errors="coerce")

            if rule.get("mode") == "fixed":
                df[column] = self._convert_series_tz(naive, rule["timezone"], target_tz)

            elif rule.get("mode") == "by_country":
                country_col = rule["country_column"]
                if country_col not in df.columns:
                    logger.warning(
                        "[%s] timezone_normalization country_column '%s' not found for '%s'. "
                        "Skipping.", table_name, country_col, column,
                    )
                    continue

                tz_names = df[country_col].map(country_tz_map)
                unresolved_mask = df[country_col].notna() & tz_names.isna()
                unresolved = df.loc[unresolved_mask, country_col].unique()
                if len(unresolved):
                    logger.warning(
                        "[%s] no timezone mapping for %s value(s): %s. '%s' will be null "
                        "for those rows.", table_name, country_col, sorted(unresolved), column,
                    )

                df[column] = self._convert_series_by_group(naive, tz_names, target_tz)

        return df


class BusinessReportBuilder:
    """
    YAML+pandas build of the unified business report -- kept for the legacy
    CSV/S3 pipeline and as the documented spec for
    sql/003_business_report_mv.sql's hand-translated SQL equivalent (see
    that file's own docstring). NOT called by Codes/postgres_pipeline.py --
    that formula/exclude logic lives in the SQL materialized view instead
    for the live Postgres path.
    """

    def __init__(self, config: CleaningConfig):
        self.config = config

    def build(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        report_cfg = self.config.business_reporting[table_name]["columns"]
        report_df = pd.DataFrame(index=df.index)

        for target_col, rule in report_cfg.items():
            if "source" in rule:
                report_df[target_col] = self._direct(df, table_name, target_col, rule["source"])
            elif rule.get("formula") == "divide":
                report_df[target_col] = self._divide(df, table_name, target_col, rule)
            elif rule.get("formula") == "duration_hours":
                report_df[target_col] = self._duration_hours(df, table_name, target_col, rule)

        return self._exclude_test_agents(report_df, table_name)

    @staticmethod
    def _direct(df: pd.DataFrame, table_name: str, target_col: str, source_col: str):
        if source_col not in df.columns:
            logger.warning(
                "[%s] business_reporting column '%s' references missing source '%s'. "
                "Filling with NA.", table_name, target_col, source_col,
            )
            return pd.NA
        return df[source_col]

    @staticmethod
    def _divide(df: pd.DataFrame, table_name: str, target_col: str, rule: dict):
        num_col, den_col = rule["numerator"], rule["denominator"]
        if num_col not in df.columns or den_col not in df.columns:
            logger.warning(
                "[%s] business_reporting column '%s' divide formula references missing column(s). "
                "Filling with NA.", table_name, target_col,
            )
            return np.nan

        num = pd.to_numeric(df[num_col], errors="coerce")
        den = pd.to_numeric(df[den_col], errors="coerce")
        # np.nan (not pd.NA): pd.NA turns the column into an "object" /
        # nullable-mixed dtype, which later breaks pd.cut() in
        # AnalyticsEngine.prepare_analytics_data() with "TypeError: boolean
        # value of NA is ambiguous". np.nan keeps the column a clean
        # float64, which pd.cut requires.
        return num.div(den).replace([np.inf, -np.inf], np.nan)

    @staticmethod
    def _duration_hours(df: pd.DataFrame, table_name: str, target_col: str, rule: dict):
        start_col, end_col = rule["start_column"], rule["end_column"]
        if start_col not in df.columns or end_col not in df.columns:
            logger.warning(
                "[%s] business_reporting column '%s' duration_hours formula references missing "
                "column(s). Filling with NA.", table_name, target_col,
            )
            return np.nan

        start = pd.to_datetime(df[start_col], errors="coerce")
        end = pd.to_datetime(df[end_col], errors="coerce")
        # Mirrors the divide-formula's np.nan guard above -- NaT arithmetic
        # already yields NaN here, and this keeps the column a clean
        # float64 rather than risking an object/pd.NA dtype.
        return (end - start).dt.total_seconds() / 3600

    def _exclude_test_agents(self, report_df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """Drop internal/QA test partner rows entirely
        (business_reporting.exclude_agent_names in cleaning_config.yml) --
        matched case/whitespace-insensitively so a stray casing variant
        doesn't slip through, consistent with apply_mapping()'s matching
        rules."""
        exclude_agent_names = self.config.business_reporting.get("exclude_agent_names", [])
        if not exclude_agent_names or "Agent_Name" not in report_df.columns:
            return report_df

        excluded_normalized = {str(name).strip().casefold() for name in exclude_agent_names}
        agent_normalized = report_df["Agent_Name"].astype("string").str.strip().str.casefold()
        is_excluded = agent_normalized.isin(excluded_normalized)

        if is_excluded.any():
            logger.info(
                "[%s] Excluded %d row(s) with a test/internal Agent_Name "
                "(business_reporting.exclude_agent_names).", table_name, is_excluded.sum(),
            )
            report_df = report_df[~is_excluded]

        return report_df


# ------------------------
# YDATA-PROFILING (kept disabled, same as the original script --
# uncomment the import at the top of transaction_report.py and this
# function if you want raw-data profiling reports back)
# ------------------------

# def run_ydata_profiling(csv_path, output_dir, report_name, title):
#     """
#     Run YData profiling report.
#     """
#     from ydata_profiling import ProfileReport
#
#     df = pd.read_csv(csv_path)
#     os.makedirs(output_dir, exist_ok=True)
#     output_path = os.path.join(output_dir, report_name)
#
#     profile = ProfileReport(df, title=title, explorative=True)
#     profile.to_file(output_path)
#
#     print(f"Report saved to: {output_path}")

# Analytics functions (prepare_analytics_data, calculate_metrics, and all
# create_*_analysis) now live in analytics.py's AnalyticsEngine.
