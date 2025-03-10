# The MIT License (MIT)
#
# Copyright (c) 2021 TU Wien
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging

import os
from tempfile import gettempdir
from pathlib import Path, PurePosixPath
import numpy as np
from typing import Union
from operator import itemgetter
import time
from typing import List, Tuple
import pandas as pd
from collections import OrderedDict
from repurpose.process import parallel_process
import traceback

from ismn.base import IsmnRoot
import ismn.const as const
from ismn.const import ismnlog
from ismn.filehandlers import DataFile, StaticMetaFile
from ismn.meta import MetaData, MetaVar, Depth


def _read_station_dir(
    root: Union[IsmnRoot, Path, str],
    stat_dir: Union[Path, str],
    temp_root: Path,
    custom_meta_reader: list,
) -> Tuple[List, List]:
    """
    Parallelizable function to read metadata for files in station dir
    """
    logger = logging.getLogger('ismn_meta_collector')

    if not isinstance(root, IsmnRoot):
        proc_root = True
        root = IsmnRoot(root)
    else:
        proc_root = False

    csv = root.find_files(stat_dir, "*.csv")

    erroneous_files = []

    try:
        if len(csv) == 0:
            raise const.IsmnFileError(
                f"Expected 1 csv file but got 0 for station {stat_dir}. "
                "Continue with empty static metadata instead.")
        else:
            if len(csv) > 1:
                logger.warning(
                    f"Expected 1 csv file but got {len(csv)} for "
                    f"station {stat_dir}. "
                    f"Use first file in dir.")
            static_meta_file = StaticMetaFile(
                root, csv[0], load_metadata=True, temp_root=temp_root)
            station_meta = static_meta_file.metadata
    except const.IsmnFileError as e:
        _csv = "*missing*" if len(csv) == 0 else csv[0]
        logger.warning(f"Error loading static meta file for {stat_dir}/{_csv}. "
                       f"We will use the placeholder metadata here. "
                       f"Error traceback: {traceback.format_exc()}")
        station_meta = MetaData(
            [MetaVar(k, v) for k, v in const.CSV_META_TEMPLATE.items()]
        )
        erroneous_files.append(os.path.join(str(root.path), str(stat_dir), str(_csv)))

    data_files = root.find_files(stat_dir, "*.stm")

    filelist = []

    for file_path in data_files:
        try:
            f = DataFile(root, file_path, temp_root=temp_root)
        except Exception as e:
            logger.error(f"Error loading ismn file {file_path}. "
                         f"We will skip this file, it might be malformed. "
                         f"Error traceback: {traceback.format_exc()}")
            erroneous_files.append(file_path)

            continue

        f.metadata.merge(station_meta, inplace=True, exclude_empty=False)

        f.metadata = f.metadata.best_meta_for_depth(
            Depth(
                f.metadata["instrument"].depth.start,
                f.metadata["instrument"].depth.end,
            ))

        # If custom metadata readers are available
        if custom_meta_reader is not None:
            for cmr in np.atleast_1d(custom_meta_reader):
                cmeta = cmr.read_metadata(f.metadata)
                if isinstance(cmeta, dict):
                    cmeta = MetaData([MetaVar(k, v) for k, v in cmeta.items()])
                if cmeta is not None:
                    f.metadata.merge(cmeta, inplace=True)

        network = f.metadata["network"].val
        station = f.metadata["station"].val

        filelist.append((network, station, f))

        logger.info(f"Processed file {file_path}")

    if proc_root:
        root.close()

    return filelist, erroneous_files


def _load_metadata_df(meta_csv_file: Union[str, Path]) -> pd.DataFrame:
    """
    Load metadata data frame from csv file
    """

    metadata_df = pd.read_csv(
        meta_csv_file,
        index_col=0,
        header=[0, 1],
        low_memory=False,
        engine="c")

    # parse date cols as datetime
    for col in ["timerange_from", "timerange_to"]:
        metadata_df[col, "val"] = pd.to_datetime(metadata_df[col, "val"])

    lvars = []
    for c in metadata_df.columns:
        if c[0] not in lvars:
            lvars.append(c[0])

    # we assume triples for all vars except these, so they must be at the end
    assert lvars[-2:] == [
        "file_path",
        "file_type",
    ], "file_type and file_path must be at the end."

    metadata_df.index.name = "idx"

    return metadata_df


class IsmnFileCollection(object):
    """
    The IsmnFileCollection class contains a list of file handlers to access data
    in the given data directory. The file list can be loaded from a previously
    stored csv file, or built by iterating over all files in the data root.
    This class also contains function to load filehandlers for certain networks
    only.

    Attributes
    ----------
    root : IsmnRoot
        Root object where data is stored.
    filelist : collections.OrderedDict
        A collection of filehandlers and network names
    temp_root : Path
        Temporary root dir.
    """

    def __init__(self, root, filelist, temp_root=gettempdir()):
        """
        Parameters
        ----------
        root : IsmnRoot
            Root object where data is stored.
        filelist : collections.OrderedDict
            A collection of filehandler stored in lists with network name as key.
        temp_root : Path or str, optional (default : gettempdir())
            Root directory where a separate subdir for temporary files
            will be created (and deleted).
        """
        self.root = root
        self.filelist = filelist
        self.temp_root = Path(temp_root)

        os.makedirs(self.temp_root, exist_ok=True)

    def __repr__(self):
        return f"{self.__class__.__name__} for {len(self.filelist.keys())} Networks"

    @classmethod
    def build_from_scratch(
            cls,
            data_root,
            parallel=True,
            log_path=None,
            temp_root=gettempdir(),
            custom_meta_readers=None,
    ):
        """
        Parameters
        ----------
        data_root : IsmnRoot or str or Path
            Root path of ISMN files or path to metadata pkl file.
            i.e. path to the downloaded zip file or the extracted zip directory (faster)
            or a file list that contains these infos already.
        parallel : bool, optional (default: True)
            Speed up metadata collecting with multiple processes.
        log_path : str or Path, optional (default: None)
            Path where the log file is created. If None is set, no log file
            will be written.
        temp_root : str or Path, (default: gettempdir())
            Temporary folder where extracted data is copied during reading from
            zip archive.
        custom_meta_readers: tuple, optional (default: None)
            Custom metadata readers
        """
        t0 = time.time()
        if isinstance(data_root, IsmnRoot):
            root = data_root
        else:
            root = IsmnRoot(data_root)

        os.makedirs(temp_root, exist_ok=True)

        log_filename = f"{root.name}.log"

        n_proc = 1 if not parallel else os.cpu_count()

        ismnlog.info(f"Collecting metadata with {n_proc} processes.")

        if not parallel:
            hint = 'Hint: Use `parallel=True` to speed up metadata ' \
                   'generation for large datasets'
        else:
            hint = ''

        print(
            f"Collecting metadata for all ismn stations in archive "
            f"{root.path}.\n"
            f"This may take a few minutes, but is only done once...\n{hint}"
        )

        process_stat_dirs = []
        for net_dir, stat_dirs in root.cont.items():
            process_stat_dirs += list(stat_dirs)

        STATIC_KWARGS = {
            'root': root.path if root.zip else root,
            'temp_root': temp_root,
            'custom_meta_reader': custom_meta_readers,
        }

        ITER_KWARGS = {
            'stat_dir': process_stat_dirs
        }

        res = parallel_process(
            _read_station_dir, ITER_KWARGS=ITER_KWARGS,
            STATIC_KWARGS=STATIC_KWARGS,
            n_proc=n_proc, show_progress_bars=True,
            ignore_errors=True, log_path=log_path,
            log_filename=log_filename, logger_name='ismn_meta_collector',
            loglevel='INFO', progress_bar_label="Stations Processed",
            backend='threading', verbose=False,
        )

        elements = []
        errors = []
        for r in res:
            elements += r[0]
            if len(r[1]) > 0:
                errors += r[1]

        elements.sort(key=itemgetter(0, 1))  # sort by net name, stat name

        filelist = OrderedDict([])
        for net, stat, fh in elements:
            if net not in filelist.keys():
                filelist[net] = []
            filelist[net].append(fh)

        t1 = time.time()
        info = f"Metadata collection finished after {int(t1-t0)} Seconds."
        if log_path is not None:
            info += (f"\nMetadata for this archive and "
                     f"Logfile stored in {log_path}")

        if len(errors) > 0:
            info += (f"\nNOTE: {len(errors)} potentially malformed file(s) "
                     f"found during metadata collection. These files will be "
                     f"ignored by the reader. "
                     f"Affected files and errors are listed in: "
                     f"{os.path.join(log_path, log_filename)}.")

            with open(os.path.join(log_path, log_filename), mode='a') as f:
                f.write("\n----- Summary: Erroneous Files -----\n")
                for e in errors:
                    f.write(f"{e}\n")
                f.write("\n-------------------------------------\n")

        ismnlog.info(info)
        print(info)

        return cls(root, filelist=filelist)

    @classmethod
    def from_metadata_df(cls, data_root, metadata_df, temp_root=gettempdir()):
        """
        Load a previously created and stored filelist from
        :func:`ismn.filecollection.IsmnFileCollection.to_metadata_csv`
        Parameters
        ----------
        data_root : IsmnRoot or str or Path
            Path where the ismn data is stored, can also be a zip file
        metadata_df : pd.DataFrame
            Metadata frame
        temp_root : str or Path, optional (default: gettempdir())
            Temporary folder where extracted data is copied during reading from
            zip archive.
        """
        if isinstance(data_root, IsmnRoot):
            root = data_root
        else:
            root = IsmnRoot(data_root)

        filelist = OrderedDict([])

        columns = np.array(list(metadata_df.columns))

        for i, row in enumerate(metadata_df.values):
            # this_nw = row.loc['network', 'val']
            vars = np.unique(columns[:-2][:, 0])
            vals = row[:-2].reshape(-1, 3)

            metadata = MetaData([
                MetaVar.from_tuple(
                    (vars[i], vals[i][2], vals[i][0], vals[i][1]))
                for i in range(len(vars))
            ])

            f = DataFile(
                root=root,
                file_path=Path(str(PurePosixPath(row[-2]))),
                load_metadata=False,
                temp_root=temp_root,
                verify_filepath=False,
                verify_temp_root=False,
            )

            f.metadata = metadata
            f.file_type = row[-1]

            this_nw = f.metadata["network"].val

            if this_nw not in filelist.keys():
                filelist[this_nw] = []

            filelist[this_nw].append(f)

        cls.metadata_df = metadata_df

        return cls(root, filelist=filelist)

    @classmethod
    def from_metadata_csv(cls,
                          data_root,
                          meta_csv_file,
                          network=None,
                          temp_root=gettempdir()):
        """
        Load a previously created and stored filelist from
        :func:`ismn.filecollection.IsmnFileCollection.to_metadata_csv`
        Parameters
        ----------
        data_root : IsmnRoot or str or Path
            Path where the ismn data is stored, can also be a zip file
        meta_csv_file : str or Path
            Csv file where the metadata is stored.
        network : list, optional (default: None)
            List of networks that are considered.
            Filehandlers for other networks are set to None.
        temp_root : str or Path, optional (default: gettempdir())
            Temporary folder where extracted data is copied during reading from
            zip archive.
        """
        if network is not None:
            network = np.atleast_1d(network)

        print(f"Using the existing ismn metadata in {meta_csv_file} to set "
              f"up ISMN_Interface. \n"
              "If there are issues with the data reader, you can remove "
              "the metadata csv file to repeat metadata collection.")

        metadata_df = _load_metadata_df(meta_csv_file)

        if network is not None:
            metadata_df = metadata_df[np.isin(metadata_df["network"].values,
                                              network)]

        metadata_df.index = range(len(metadata_df.index))

        return cls.from_metadata_df(
            data_root, metadata_df, temp_root=temp_root)

    def to_metadata_csv(self, meta_csv_file):
        """
        Write filehandle metadata from filelist to metdata csv that contains
        ALL metadata / variables of the filehander.
        Can be read back in as filelist with filehandlers using
        :func:`ismn.filecollection.IsmnFileCollection.from_metadata_csv`.

        Parameters
        ----------
        meta_csv_file : Path or str, optional (default: None)
            Directory where the csv file with the correct name is crated
        """

        dfs = []

        for i, filehandler in enumerate(self.iter_filehandlers()):
            df = filehandler.metadata.to_pd(True, dropna=False)
            df[("file_path",
                "val")] = str(PurePosixPath(filehandler.file_path))
            df[("file_type", "val")] = filehandler.file_type

            df.index = [i]
            dfs.append(df)
            i += 1

        dfs = pd.concat(dfs, axis=0, sort=True)
        cols_end = ["file_path", "file_type"]

        dfs = dfs[[c for c in dfs.columns if c[0] not in cols_end] +
                  [c for c in dfs.columns if c[0] in cols_end]]
        dfs = dfs.infer_objects().fillna(np.nan)

        os.makedirs(Path(os.path.dirname(meta_csv_file)), exist_ok=True)
        dfs.to_csv(meta_csv_file)

    def get_filehandler(self, idx):
        """
        Get the nth filehandler in a list of all filehandlers for all networks.
        e.g. if there are 2 networks, with 3 filehandlers/sensors each, idx=4
        will return the first filehandler of the second network.

        Parameters
        ----------
        idx: int
            Index of filehandler to read.

        Returns
        -------
        filehandler : DataFile
            nth filehandler of all filehandlers in the sorted list.
        """
        fs = 0
        for net, files in self.filelist.items():
            l = len(files)
            if fs + l > idx:
                return files[idx - fs]
            else:
                fs += l

    def iter_filehandlers(self, networks=None):
        """
        Iterator over files for networks

        Parameters
        ----------
        networks : list, optional (default: None)
            Name of networks to get files for, or None to use all networks.

        Yields
        -------
        file : DataFile
            Filehandler with metadata
        """
        for net, files in self.filelist.items():
            if (networks is None) or (net in networks):
                for f in files:
                    yield f
        yield from ()  # in case networks is an empty list

    def close(self):
        # close root and all filehandlers
        self.root.close()
        for f in self.iter_filehandlers():
            f.close()
