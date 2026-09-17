"""SOBA Parquet validation — vendored from the consortium gist.

Source: the validator gist shared with the project (``gistfile1.py`` in the shared folder),
kept verbatim apart from this header and one marked change: the mandatory reference columns are
built per reference source (``scat_``/``swot_``) instead of the retired ``ref_lon``,
``ref_lat`` and ``ref_time``, following SOBA spec v1.1.0. The change sits between the
``--- local change`` markers in ``SOBAValidationRules``. It is the acceptance gate for delivered
TEST files: ``--validate`` runs it against the parquet the tool has just written, and
``--validator PATH`` runs a newer copy instead of this one. Refresh by re-copying the gist and
reapplying the marked change.

validate_soba_parquet.py
Validation script for SOBA Parquet files - supports both IW and WV acquisition modes
"""

import os
import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Union
import argparse
from datetime import datetime
import re


class SOBAValidationRules:
    """Class containing validation rules for different SAR modes"""
    
    # Common mandatory columns for both IW and WV
    COMMON_MANDATORY = {
        'primary_key': {
            'dtype': 'object',
            'description': 'Unique identifier: sar-safe + ref_lon + ref_lat',
            'nullable': False,
            'pattern': r'^S1[ABCD]_(IW|WV)_(GRD|SLC|OCN).*\.SAFE.*_-?\d+\.\d_-?\d+\.\d$'
        },
        'sar_time': {
            'dtype': 'datetime64[ns]',
            'description': 'Start time of the SAR measurement',
            'nullable': False
        },
        'sar_lat': {
            'dtype': 'float32',
            'description': 'Latitude on the centroid [°]',
            'nullable': False,
            'range': [-90, 90]
        },
        'sar_lon': {
            'dtype': 'float32',
            'description': 'Longitude on the centroid [°]',
            'nullable': False,
            'range': [-180, 180]
        },
        'sar_incidence_angle': {
            'dtype': 'float32',
            'description': 'Angle of the centroid wrt nadir [°]',
            'nullable': False,
            'range': [0, 90]
        },
        'sar_elevation_angle': {
            'dtype': 'float32',
            'description': 'Angle of the centroid within the antenna diagram [°]',
            'nullable': False,
            'range': [0, 90]
        },
        'sar_ground_heading': {
            'dtype': 'float32',
            'description': 'Averaged ground heading [°]',
            'nullable': False,
            'range': [0, 360]
        },
        'sar_distance_to_coast': {
            'dtype': 'float32',
            'description': 'Distance to closest coastline [km]',
            'nullable': False,
            'range': [0, 2689] # point némo
        },
        'sar_path_ocn': {
            'dtype': 'object',
            'description': 'Full path of OCN measurement',
            'nullable': False
        }
    }
    
    # IW-specific mandatory columns
    IW_SPECIFIC = {
        'sar_path_slc': {
            'dtype': 'object',
            'description': 'Full path of SLC measurement + swath identifier',
            'nullable': False,
            'pattern': r'.*\.SAFE:IW[1-3]$'
        },
        'sar_path_grd': {
            'dtype': 'object',
            'description': 'Full path of GRD measurement',
            'nullable': False
        },
        'sar_safe_grd': {
            'dtype': 'object',
            'description': 'Sentinel-1 GRD SAFE name',
            'nullable': False,
            'pattern': r'^S1[ABCD]_IW_GRDH_1S[DVSH]{2}_.*\.SAFE$'
        },
        'sar_safe_slc': {
            'dtype': 'object',
            'description': 'Sentinel-1 SLC SAFE name + swath identifier',
            'nullable': False,
            'pattern': r'^S1[ABCD]_IW_SLC__1S[DVSH]{2}_.*\.SAFE:IW[1-3]$'
        },
        'sar_safe_ocn': {
            'dtype': 'object',
            'description': 'Sentinel-1 OCN SAFE name',
            'nullable': False,
            'pattern': r'^S1[ABCD]_IW_OCN__2S[DVSH]{2}_.*\.SAFE$'
        }
    }
    
    # WV-specific mandatory columns
    WV_SPECIFIC = {
        'sar_path_slc': {
            'dtype': 'object',
            'description': 'Full path of SLC measurement',
            'nullable': False
        },
        'sar_safe_slc': {
            'dtype': 'object',
            'description': 'Sentinel-1 SLC SAFE name + WV imagette number',
            'nullable': False,
            'pattern': r'^S1[ABCD]_WV_SLC__1S[SVH]{2}_.*\.SAFE:WV_\d+$'
        },
        'sar_safe_ocn': {
            'dtype': 'object',
            'description': 'Sentinel-1 OCN SAFE name + WV imagette number',
            'nullable': False,
            'pattern': r'^S1[ABCD]_WV_OCN__2S[SVH]{2}_.*\.SAFE:WV_\d+$'
        }
    }
    
    # Columns for challenger prediction dataset (both IW and WV)
    CHALLENGER_COLUMNS = {
        'primary_key': {
            'dtype': 'object',
            'description': 'Unique identifier',
            'nullable': False
        },
        # Additional columns for prediction (can be any name)
        # Example: 'Hs_predicted', 'wind_speed_predicted', etc.
    }
    
    # --- local change (SOBA spec v1.1.0) ------------------------------------- #
    REFERENCE_SOURCES = ('scat', 'swot')

    REFERENCE_COLUMN_SUFFIXES = {
        '_lon': {
            'dtype': 'float32',
            'description': 'Longitude of the reference measurement [°]',
            'nullable': False,
            'range': [-180, 180]
        },
        '_lat': {
            'dtype': 'float32',
            'description': 'Latitude of the reference measurement [°]',
            'nullable': False,
            'range': [-90, 90]
        },
        '_time': {
            'dtype': 'datetime64[ns]',
            'description': 'Time of reference measurement',
            'nullable': False
        }
    }

    @classmethod
    def normalise_references(cls, reference='scat') -> tuple:
        """One or more reference sources, from a string ('scat,swot') or an iterable"""
        if reference is None or reference == '':
            return ('scat',)
        if isinstance(reference, str):
            sources = [part.strip().lower() for part in reference.split(',')]
        else:
            sources = [str(part).strip().lower() for part in reference]
        sources = [source for source in sources if source]
        if not sources:
            return ('scat',)
        unknown = [source for source in sources if source not in cls.REFERENCE_SOURCES]
        if unknown:
            raise ValueError(
                f"unsupported reference source(s) {', '.join(unknown)}; expected one or more "
                f"of {', '.join(cls.REFERENCE_SOURCES)}"
            )
        return tuple(dict.fromkeys(sources))

    @classmethod
    def get_reference_columns(cls, reference='scat') -> Dict:
        """Mandatory reference columns for the given reference source(s)

        A crossing can carry more than one reference — a scatterometer and SWOT, say — so the
        rule table is the union over the sources given.
        """
        columns = {}
        for source in cls.normalise_references(reference):
            columns.update({
                f"{source}{suffix}": dict(spec)
                for suffix, spec in cls.REFERENCE_COLUMN_SUFFIXES.items()
            })
        return columns
    # --- end local change ---------------------------------------------------- #

    @classmethod
    def get_rules_for_mode(cls, mode: str, reference='scat') -> Dict:
        """Get validation rules for a SAR mode and reference source"""
        reference_columns = cls.get_reference_columns(reference)
        if mode.upper() == 'IW':
            return {**cls.COMMON_MANDATORY, **reference_columns, **cls.IW_SPECIFIC}
        elif mode.upper() == 'WV':
            return {**cls.COMMON_MANDATORY, **reference_columns, **cls.WV_SPECIFIC}
        else:
            raise ValueError(f"Unsupported SAR mode: {mode}. Must be IW or WV.")
    
    @classmethod
    def get_expected_patterns(cls, mode: str) -> Dict:
        """Get expected filename patterns for validation"""
        if mode.upper() == 'IW':
            return {
                'sar_path_slc': r'.*\.SAFE:IW[1-3]$',
                'sar_safe_grd': r'^S1[ABCD]_IW_GRDH_1S[DVSH]{2}_.*\.SAFE$',
                'sar_safe_slc': r'^S1[ABCD]_IW_SLC__1S[DVSH]{2}_.*\.SAFE:IW[1-3]$',
                'sar_safe_ocn': r'^S1[ABCD]_IW_OCN__2S[DVSH]{2}_.*\.SAFE$'
            }
        elif mode.upper() == 'WV':
            return {
                'sar_safe_slc': r'^S1[ABCD]_WV_SLC__1S[SVH]{2}_.*\.SAFE:WV_\d+$',
                'sar_safe_ocn': r'^S1[ABCD]_WV_OCN__2S[SVH]{2}_.*\.SAFE:WV_\d+$'
            }
        return {}


class SOBAParquetValidator:
    """Validator for SOBA Parquet files supporting IW and WV modes"""
    
    def __init__(self, mode: str = 'WV', verbose: bool = False, 
                 dataset_type: str = 'test', reference: str = 'scat'):
        """
        Initialize validator
        
        Parameters:
        -----------
        mode : str
            SAR acquisition mode ('IW' or 'WV')
        verbose : bool
            Enable verbose logging
        dataset_type : str
            Type of dataset ('test' or 'challenger')
        reference : str or iterable
            Reference source(s) whose columns are mandatory — 'scat' or 'swot',
            comma-separated or as an iterable when the file carries both
        """
        self.mode = mode.upper()
        self.dataset_type = dataset_type.lower()
        self.verbose = verbose
        self.reference = SOBAValidationRules.normalise_references(reference)
        self.rules = SOBAValidationRules.get_rules_for_mode(self.mode, self.reference)
        
        self.results = {
            'file_path': None,
            'mode': self.mode,
            'dataset_type': self.dataset_type,
            'reference': ','.join(self.reference),
            'valid': False,
            'errors': [],
            'warnings': [],
            'info': [],
            'statistics': {}
        }
        self._setup_logging()
        
    def _setup_logging(self):
        """Setup logging configuration"""
        if self.verbose:
            logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
        else:
            logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    def _detect_mode_from_filename(self, filename: str) -> Optional[str]:
        """Detect SAR mode from filename"""
        if 'IW' in filename:
            return 'IW'
        elif 'WV' in filename:
            return 'WV'
        return None
    
    def _detect_mode_from_dataframe(self, df: pd.DataFrame) -> Optional[str]:
        """Detect SAR mode from dataframe columns"""
        # Check for IW-specific columns
        if all(col in df.columns for col in ['sar_safe_grd', 'sar_path_grd']):
            return 'IW'
        # Check for WV-specific patterns in sar_safe_slc
        if 'sar_safe_slc' in df.columns:
            sample = str(df['sar_safe_slc'].iloc[0]) if len(df) > 0 else ''
            if 'WV_' in sample:
                return 'WV'
            elif 'IW' in sample:
                return 'IW'
        return None
    
    def validate_file(self, file_path: str) -> Dict[str, Any]:
        """
        Validate a SOBA Parquet file
        
        Parameters:
        -----------
        file_path : str
            Path to the Parquet file to validate
        
        Returns:
        --------
        Dict containing validation results
        """
        self.results['file_path'] = file_path
        self.results['errors'] = []
        self.results['warnings'] = []
        self.results['info'] = []
        
        logging.info(f"Validating file: {file_path}")
        
        # Step 1: Check if file exists
        if not os.path.exists(file_path):
            error_msg = f"File does not exist: {file_path}"
            logging.error(error_msg)
            self.results['errors'].append(error_msg)
            self.results['valid'] = False
            return self.results
        
        # Step 2: Try to read the file
        try:
            df = pd.read_parquet(file_path)
            logging.info(f"Successfully read Parquet file with {len(df)} rows and {len(df.columns)} columns")
            self.results['info'].append(f"Rows: {len(df)}, Columns: {len(df.columns)}")
        except Exception as e:
            error_msg = f"Failed to read Parquet file: {e}"
            logging.error(error_msg)
            self.results['errors'].append(error_msg)
            self.results['valid'] = False
            return self.results
        
        # Step 3: Auto-detect mode if not specified
        detected_mode = self._detect_mode_from_dataframe(df)
        if detected_mode and self.mode != detected_mode:
            warning_msg = f"Mode specified as {self.mode} but detected {detected_mode} from data"
            logging.warning(warning_msg)
            self.results['warnings'].append(warning_msg)
            self.mode = detected_mode
            self.rules = SOBAValidationRules.get_rules_for_mode(self.mode, self.reference)
            logging.info(f"Switching to {self.mode} mode for validation")
        
        # Step 4: Validate based on dataset type
        if self.dataset_type == 'test':
            self._validate_test_dataset(df)
        elif self.dataset_type == 'challenger':
            self._validate_challenger_dataset(df)
        else:
            error_msg = f"Unsupported dataset type: {self.dataset_type}. Must be 'test' or 'challenger'"
            logging.error(error_msg)
            self.results['errors'].append(error_msg)
        
        # Step 5: Additional generic checks
        self._validate_data_types(df)
        self._validate_ranges(df)
        self._validate_primary_key(df)
        self._validate_patterns(df)
        self._validate_null_values(df)
        self._generate_statistics(df)
        
        # Determine overall validity
        self.results['valid'] = len(self.results['errors']) == 0
        
        if self.results['valid']:
            logging.info("✅ Validation PASSED")
        else:
            logging.error(f"❌ Validation FAILED with {len(self.results['errors'])} errors")
        
        if self.results['warnings']:
            logging.warning(f"⚠️ {len(self.results['warnings'])} warnings")
        
        return self.results
    
    def _validate_test_dataset(self, df: pd.DataFrame):
        """Validate TEST dataset (contains all mandatory columns)"""
        logging.info("Validating as TEST dataset")
        self.results['info'].append("Dataset type: TEST")
        
        # Check for all mandatory variables
        missing_vars = []
        for var in self.rules.keys():
            if var not in df.columns:
                missing_vars.append(var)
        
        if missing_vars:
            error_msg = f"Missing mandatory variables for TEST dataset: {', '.join(missing_vars)}"
            logging.error(error_msg)
            self.results['errors'].append(error_msg)
        else:
            logging.info("All mandatory TEST variables are present")
            self.results['info'].append("All mandatory TEST variables present")
    
    def _validate_challenger_dataset(self, df: pd.DataFrame):
        """Validate CHALLENGER dataset (minimal columns)"""
        logging.info("Validating as CHALLENGER dataset")
        self.results['info'].append("Dataset type: CHALLENGER")
        
        # Check for minimal required columns
        challenger_required = ['primary_key']
        # Check for prediction columns (any column with 'predicted' in name or custom)
        # For now, we just check if primary_key exists
        for var in challenger_required:
            if var not in df.columns:
                error_msg = f"Missing required column for CHALLENGER dataset: {var}"
                logging.error(error_msg)
                self.results['errors'].append(error_msg)
        
        # Check that there is at least one prediction column
        prediction_cols = [col for col in df.columns if col not in ['primary_key']]
        if not prediction_cols:
            warning_msg = "No prediction columns found in CHALLENGER dataset"
            logging.warning(warning_msg)
            self.results['warnings'].append(warning_msg)
        else:
            logging.info(f"Found {len(prediction_cols)} prediction columns")
            self.results['info'].append(f"Prediction columns: {', '.join(prediction_cols[:5])}")
    
    def _validate_data_types(self, df: pd.DataFrame):
        """Validate data types (accepts 'str' as valid for 'object' type)"""
        for var, specs in self.rules.items():
            if var not in df.columns:
                continue
            
            expected_dtype = specs.get('dtype')
            actual_dtype = str(df[var].dtype)
            
            # Check if dtype matches (with flexibility for string types)
            dtype_match = False
            
            # STRING TYPES: accept 'object', 'str', 'string' interchangeably
            if expected_dtype == 'object':
                dtype_match = actual_dtype in ['object', 'string', 'str']
            elif expected_dtype in ['str', 'string']:
                dtype_match = actual_dtype in ['object', 'string', 'str']
            elif expected_dtype == 'datetime64[ns]':
                dtype_match = 'datetime64' in actual_dtype
            elif expected_dtype in ['float32', 'float64']:
                dtype_match = 'float' in actual_dtype
            elif expected_dtype in ['int32', 'int64']:
                dtype_match = 'int' in actual_dtype
            elif expected_dtype == 'bool':
                dtype_match = 'bool' in actual_dtype
            else:
                # Fallback: exact match
                dtype_match = actual_dtype == expected_dtype
            
            if not dtype_match:
                warning_msg = f"Variable '{var}' has dtype {actual_dtype}, expected {expected_dtype}"
                logging.warning(warning_msg)
                self.results['warnings'].append(warning_msg)
            else:
                # If it's a string type, log it as info (not warning)
                if expected_dtype == 'object' and actual_dtype in ['str', 'string']:
                    logging.debug(f"Variable '{var}' uses '{actual_dtype}' (accepted as object)")
    
    def _validate_ranges(self, df: pd.DataFrame):
        """Validate data ranges"""
        for var, specs in self.rules.items():
            if var not in df.columns:
                continue
            
            range_constraint = specs.get('range')
            if range_constraint is not None:
                min_val, max_val = range_constraint
                valid_values = df[var].dropna()
                if len(valid_values) > 0:
                    out_of_range = valid_values[(valid_values < min_val) | (valid_values > max_val)]
                    if len(out_of_range) > 0:
                        pct = (len(out_of_range) / len(valid_values)) * 100
                        warning_msg = f"Variable '{var}' has {len(out_of_range)} values ({pct:.1f}%) outside range [{min_val}, {max_val}]"
                        logging.warning(warning_msg)
                        self.results['warnings'].append(warning_msg)
    
    def _validate_primary_key(self, df: pd.DataFrame):
        """Validate primary key format and uniqueness"""
        if 'primary_key' not in df.columns:
            self.results['errors'].append("Primary key column not found")
            return
        
        # Check uniqueness
        if len(df['primary_key']) != len(df['primary_key'].unique()):
            duplicates = len(df['primary_key']) - len(df['primary_key'].unique())
            error_msg = f"Primary key has {duplicates} duplicate values"
            logging.error(error_msg)
            self.results['errors'].append(error_msg)
        else:
            logging.info("Primary key is unique")
            self.results['info'].append("Primary key is unique")
        
        # Check format
        pattern = self.rules.get('primary_key', {}).get('pattern')
        if pattern:
            invalid_keys = []
            for key in df['primary_key'].head(1000):
                if not re.match(pattern, str(key)):
                    invalid_keys.append(key)
            
            if invalid_keys:
                warning_msg = f"Found {len(invalid_keys)} primary keys with invalid format (first 1000 checked)"
                logging.warning(warning_msg)
                self.results['warnings'].append(warning_msg)
    
    def _validate_patterns(self, df: pd.DataFrame):
        """Validate column values against expected patterns"""
        patterns = SOBAValidationRules.get_expected_patterns(self.mode)
        
        for col, pattern in patterns.items():
            if col not in df.columns:
                continue
            
            invalid_values = []
            for value in df[col].head(1000):  # Check first 1000
                if pd.notna(value) and not re.match(pattern, str(value)):
                    invalid_values.append(value)
            
            if invalid_values:
                pct = (len(invalid_values) / min(1000, len(df[col]))) * 100
                warning_msg = f"Column '{col}' has {len(invalid_values)} values ({pct:.1f}%) not matching expected pattern"
                logging.warning(warning_msg)
                self.results['warnings'].append(warning_msg)
    
    def _validate_null_values(self, df: pd.DataFrame):
        """Check for null values in mandatory variables"""
        for var, specs in self.rules.items():
            if var not in df.columns:
                continue
            
            nullable = specs.get('nullable', False)
            null_count = df[var].isnull().sum()
            
            if null_count > 0:
                if not nullable:
                    error_msg = f"Variable '{var}' has {null_count} null values but should not be nullable"
                    logging.error(error_msg)
                    self.results['errors'].append(error_msg)
                else:
                    pct = (null_count / len(df)) * 100
                    if pct > 50:  # More than 50% null
                        warning_msg = f"Variable '{var}' has {null_count} null values ({pct:.1f}%)"
                        logging.warning(warning_msg)
                        self.results['warnings'].append(warning_msg)
    
    def _generate_statistics(self, df: pd.DataFrame):
        """Generate basic statistics"""
        stats = {
            'total_rows': len(df),
            'total_columns': len(df.columns),
            'memory_usage_gb': df.memory_usage(deep=True).sum() / 1024**3,
            'mode': self.mode
        }
        
        # Count missing values
        total_cells = len(df) * len(df.columns)
        missing_cells = df.isnull().sum().sum()
        stats['missing_percentage'] = (missing_cells / total_cells) * 100 if total_cells > 0 else 0
        
        self.results['statistics'] = stats
    
    def print_report(self):
        """Print formatted validation report"""
        print("\n" + "="*80)
        print("SOBA PARQUET VALIDATION REPORT")
        print("="*80)
        print(f"File: {self.results['file_path']}")
        print(f"Mode: {self.results['mode']}")
        print(f"Dataset Type: {self.results['dataset_type']}")
        print(f"Status: {'✅ PASSED' if self.results['valid'] else '❌ FAILED'}")
        print("-"*80)
        
        if self.results['errors']:
            print("\n❌ ERRORS:")
            for error in self.results['errors']:
                print(f"  - {error}")
        
        if self.results['warnings']:
            print("\n⚠️ WARNINGS:")
            for warning in self.results['warnings']:
                print(f"  - {warning}")
        
        if self.results['info']:
            print("\nℹ️ INFO:")
            for info in self.results['info']:
                print(f"  - {info}")
        
        if self.results['statistics']:
            print("\n📊 STATISTICS:")
            for key, value in self.results['statistics'].items():
                if isinstance(value, float):
                    print(f"  - {key}: {value:.2f}")
                else:
                    print(f"  - {key}: {value}")
        
        print("="*80 + "\n")


def validate_parquet_files(file_or_dir: str, mode: str = 'WV', 
                          dataset_type: str = 'test',
                          pattern: str = "*.parquet", 
                          verbose: bool = False,
                          reference: str = 'scat') -> List[Dict]:
    """
    Validate one or multiple Parquet files
    
    Parameters:
    -----------
    file_or_dir : str
        Path to single file or directory
    mode : str
        SAR mode ('IW' or 'WV')
    dataset_type : str
        Dataset type ('test' or 'challenger')
    pattern : str
        Glob pattern for files (if directory)
    verbose : bool
        Enable verbose output
    
    Returns:
    --------
    List of validation results
    """
    import glob
    
    # Determine if input is file or directory
    if os.path.isfile(file_or_dir):
        files = [file_or_dir]
    else:
        search_pattern = os.path.join(file_or_dir, pattern)
        files = glob.glob(search_pattern)
        
        if not files:
            logging.warning(f"No files found matching pattern: {search_pattern}")
            return []
    
    logging.info(f"Found {len(files)} file(s) to validate")
    
    validator = SOBAParquetValidator(mode=mode, verbose=verbose, 
                                   dataset_type=dataset_type, reference=reference)
    results = []
    
    for file_path in sorted(files):
        result = validator.validate_file(file_path)
        results.append(result)
        validator.print_report()
    
    # Summary
    if len(results) > 1:
        total = len(results)
        passed = sum(1 for r in results if r['valid'])
        failed = total - passed
        
        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        print(f"Total files: {total}")
        print(f"✅ Passed: {passed}")
        print(f"❌ Failed: {failed}")
        print("="*80 + "\n")
    
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate SOBA Parquet files (supports IW and WV modes)"
    )
    parser.add_argument("file_or_dir", 
                       help="Path to Parquet file or directory")
    parser.add_argument("--mode", type=str, choices=['IW', 'WV'], default='WV',
                       help="SAR acquisition mode (IW or WV)")
    parser.add_argument("--dataset-type", type=str, choices=['test', 'challenger'], 
                       default='test',
                       help="Dataset type (test or challenger)")
    parser.add_argument("--reference", type=str, default='scat',
                       help="Reference source(s) whose columns are mandatory: scat or swot, "
                            "comma-separated for both (default: scat)")
    parser.add_argument("--pattern", type=str, default="*.parquet",
                       help="Pattern for matching files in directory")
    parser.add_argument("--verbose", action="store_true",
                       help="Enable verbose output")
    parser.add_argument("--auto-detect", action="store_true",
                       help="Auto-detect mode from filename/data")
    parser.add_argument("--output-report", type=str,
                       help="Save validation report to JSON file")
    
    args = parser.parse_args()
    
    # Auto-detect mode if requested
    if args.auto_detect:
        # Try to detect mode from the first file
        import glob
        if os.path.isdir(args.file_or_dir):
            pattern = os.path.join(args.file_or_dir, args.pattern)
            files = glob.glob(pattern)
            if files:
                filename = os.path.basename(files[0])
                if 'IW' in filename:
                    args.mode = 'IW'
                elif 'WV' in filename:
                    args.mode = 'WV'
                print(f"Auto-detected mode: {args.mode}")
        else:
            filename = os.path.basename(args.file_or_dir)
            if 'IW' in filename:
                args.mode = 'IW'
            elif 'WV' in filename:
                args.mode = 'WV'
            print(f"Auto-detected mode: {args.mode}")
    
    # Run validation
    results = validate_parquet_files(
        args.file_or_dir,
        mode=args.mode,
        dataset_type=args.dataset_type,
        pattern=args.pattern,
        verbose=args.verbose,
        reference=args.reference
    )
    
    # Save report if requested
    if args.output_report and results:
        import json
        with open(args.output_report, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"Report saved to {args.output_report}")