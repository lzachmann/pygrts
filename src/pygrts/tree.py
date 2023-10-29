import typing as T
from abc import ABC, abstractmethod
from collections import Counter, namedtuple

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import CRS
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon, box
from sklearn.cluster import KMeans

BBox = namedtuple('BBox', 'left bottom right top')
ID_COLUMN = 'uid'


@pd.api.extensions.register_dataframe_accessor('grts')
class GRTSFrame:
    def __init__(self, obj):
        self._obj = obj
        data = np.c_[self._obj.geometry.x.values, self._obj.geometry.y.values]
        self.tree = cKDTree(data)

    def query_points(self, points: np.ndarray, k: int = 1) -> gpd.GeoDataFrame:
        """
        Returns:
            (distances, indices, mask)
        """
        distances, indices = self.tree.query(points, k=k)
        df = self._obj.iloc[indices]
        df = df.assign(point_distance=distances)

        return df


class TreeMixin(ABC):
    def __init__(self, dataframe):
        self.dataframe = dataframe
        self.sindex = self.dataframe.sindex

    @property
    def crs(self):
        """Get the GeoDataFrame CRS."""
        return self.dataframe.crs

    def create_poly(self, bounds: T.Sequence[float]) -> Polygon:
        """Creates a Polygon geometry.

        Args:
            bounds (tuple): Left, bottom, right, top.

        Returns:
            ``shapely.geometry.Polygon``
        """
        return box(*bounds)

    def intersection(self, geometry: Polygon) -> T.List[int]:
        """Gets the intersection of a polygon geometry."""
        return list(self.sindex.intersection(geometry))

    def iter_samples(
        self, df_sample: gpd.GeoDataFrame
    ) -> T.Tuple[Point, T.List[int]]:
        for row in df_sample.itertuples():
            yield row.geometry, list(self.sindex.query(row.geometry))

    @abstractmethod
    def to_geom(self):
        pass

    @abstractmethod
    def to_frame(self):
        pass


class QuadTree(TreeMixin):
    """A class to generate a QuadTree.

    Args:
        dataframe (GeoDataFrame): The ``geopandas.GeoDataFrame`` with data samples.
        force_square (Optional[bool]): Whether to force square quadrants. Default is ``True``.
    """

    def __init__(self, dataframe: gpd.GeoDataFrame, force_square: bool = True):
        super(QuadTree, self).__init__(dataframe)

        if dataframe.crs == CRS(4326):
            offset = 0.5
        else:
            offset = 20_000
        total_bounds = self.dataframe.total_bounds.flatten().tolist()
        total_bounds[0] -= offset
        total_bounds[1] -= offset
        total_bounds[2] += offset
        total_bounds[3] += offset
        bounds_names = self.bounds_to_tuple(total_bounds)

        # Initiate the tree as the total bounds
        self.tree_bounds = [total_bounds]
        self.tree_ids = ['']
        self.clusters = None

        if force_square:
            # Update the grid to force quadrants of equal length
            if self.min_qside == 'y':
                total_bounds = (
                    bounds_names.left,
                    bounds_names.top - abs(self.qx_len),
                    bounds_names.right,
                    bounds_names.top,
                )
            else:
                total_bounds = (
                    bounds_names.left,
                    bounds_names.bottom,
                    bounds_names.left + abs(self.qy_len),
                    bounds_names.top,
                )

            self.tree_bounds = [total_bounds]

    def __iter__(self):
        return self

    def __next__(self):
        self.split()

    def __len__(self) -> int:
        return self.nquads

    @property
    def nquads(self) -> int:
        """Get the number of quadrants in the tree."""
        return len(self.tree)

    @property
    def tree(self) -> T.Sequence[Polygon]:
        """Get the quadrant tree geometry."""
        return self.to_geom()

    @staticmethod
    def bounds_to_tuple(bounds: T.Sequence[float]) -> BBox:
        return BBox(*bounds)

    @property
    def min_qside(self) -> str:
        """Get the minimum quadrant side (y or x)"""
        return 'y' if self.qy_len < self.qx_len else 'x'

    @property
    def qy_len(self) -> float:
        """Get the quadrant latitudinal length."""
        bbox = self.bounds_to_tuple(self.tree_bounds[0])
        return bbox.top - bbox.bottom

    @property
    def qx_len(self) -> float:
        """Get the quadrant longitudinal length."""
        bbox = self.bounds_to_tuple(self.tree_bounds[0])
        return bbox.right - bbox.left

    @property
    def qmin(self):
        """Get the minimum quadrant length."""
        return self.qy_len if self.min_qside == 'y' else self.qx_len

    @property
    def qmax(self) -> float:
        """Get the maximum quadrant length."""
        return self.qy_len if self.min_qside == 'x' else self.qx_len

    @property
    def bounds(self) -> BBox:
        """Get the tree bounds."""
        frame = self.to_frame().total_bounds.flatten().tolist()

        return self.bounds_to_tuple(frame)

    def to_geom(self) -> T.List[Polygon]:
        """Converts quadrant bounds to geometry."""
        return [self.create_poly(bbox) for bbox in self.tree_bounds]

    def to_frame(self) -> gpd.GeoDataFrame:
        """Converts tree quadrants to a DataFrame."""
        return gpd.GeoDataFrame(
            data=self.tree_ids,
            geometry=self.to_geom(),
            crs=self.crs,
            columns=[ID_COLUMN],
        )

    @property
    def counts(self) -> T.Dict[str, int]:
        """Get counts of sample occurrences in each quadrant."""
        counts: T.Dict[str, int] = {}
        for i, geom in zip(self.tree_ids, self.tree):
            point_int = self.intersection(geom.bounds)
            if point_int:
                counts[i] = len(point_int)

        return counts

    def count(self, qid: str) -> int:
        """Counts sample occurrences in a quadrant.

        Args:
            qid (str): The quadrant id.
        """

        bbox = (
            self.to_frame()
            .query(f"id == '{qid}'")
            .geometry.bounds.values.flatten()
            .tolist()
        )

        # Get points that intersect the quadrant
        point_int = self.intersection(bbox)

        return len(point_int) if point_int else 0

    def split(self, thresh: int = 0) -> "QuadTree":
        """Splits a tree into quadrants.

        1 | 3
        --|--
        0 | 2

        Args:
            thresh (int): The sample threshold to remove a quadrant. Default is 0, or remove a
                quadrant if it is empty.
        """
        new_tree_bounds: T.List[namedtuple] = []
        new_tree_ids: T.List[str] = []

        self.contains_null = False

        for qi, quad in enumerate(self.tree):
            left, bottom, right, top = quad.bounds
            xcenter = left + (right - left) / 2.0
            ycenter = top - (top - bottom) / 2.0

            quad_id = self.tree_ids[qi]
            qdict = {
                # lower left
                0: (left, bottom, xcenter, ycenter),
                # upper left
                1: (left, ycenter, xcenter, top),
                # lower right
                2: (xcenter, bottom, right, ycenter),
                # upper right
                3: (xcenter, ycenter, right, top),
            }

            for qid, bbox in qdict.items():
                id_list = self.intersection(bbox)
                if id_list:
                    if len(id_list) > thresh:
                        new_tree_bounds.append(bbox)
                        new_tree_ids.append(f"{quad_id}{qid}")
                    else:
                        self.contains_null = True

                else:
                    self.contains_null = True

        self.tree_bounds = new_tree_bounds
        self.tree_ids = new_tree_ids

        return self

    def split_recursive(
        self,
        max_samples: int = None,
        max_length: int = None,
        first_null: bool = False,
        min_thresh: int = 0,
    ) -> None:
        """Splits quadrants recursively.

        Args:
            max_samples (Optional[int]): The maximum number of samples.
            max_length (Optional[float]): The maximum length of a quadrant side. Overrides ``max_samples``.
            first_null (Optional[bool]): Whether to break on the first null quadrant. Overrides ``max_samples``.
            min_thresh (Optional[bool]): The minimum samples within a quadrant to remove the quadrant. Note that
                this is a keyword argument passed to ``self.split``. It is not a stopping criterion for
                recursive splitting.

        Preference:
            first_null > max_length > max_samples > min_samples
        """
        if first_null:
            max_length = None
            max_samples = None

        elif isinstance(max_length, float) or isinstance(max_length, int):
            first_null = False
            max_samples = None

        elif isinstance(max_samples, int):
            if not isinstance(max_samples, int):
                raise NameError('One of the four options must be chosen.')

            first_null = False
            max_length = None

        old_count = 1e9

        while True:
            self.split(thresh=min_thresh)

            if isinstance(max_length, float) or isinstance(max_length, int):
                if self.qmax <= max_length:
                    break

            elif first_null:
                if self.contains_null:
                    break

            elif isinstance(max_samples, int):
                max_count = self.counts[max(self.counts, key=self.counts.get)]

                if max_count <= max_samples:
                    break

                if max_count == old_count:
                    break

                old_count = max_count

    def weight_grids(
        self, n_clusters: int = 10, num_results: int = 2
    ) -> gpd.GeoDataFrame:
        """Weights grids for sampling.

        Args:
            n_clusters (Optional[int]): The number of clusters.
            num_results (Optional[int]): The number of result near cluster centers.

        Returns:
            ``geopandas.DataFrame``
        """
        qt_frame = self.to_frame()

        # Get coordinates
        X = np.c_[qt_frame.centroid.x.values, qt_frame.centroid.y.values]

        # Fit a KMeans
        kmeans = KMeans(n_clusters=n_clusters).fit(X)

        self.clusters = gpd.GeoDataFrame(
            data=kmeans.predict(X),
            geometry=self.to_geom(),
            crs=self.crs,
            columns=['cluster'],
        )

        # Get the n nearest grids to the cluster centers
        for cluster_index in range(0, kmeans.cluster_centers_.shape[0]):
            bounds = (
                kmeans.cluster_centers_[cluster_index, 0],
                kmeans.cluster_centers_[cluster_index, 1],
                kmeans.cluster_centers_[cluster_index, 0],
                kmeans.cluster_centers_[cluster_index, 1],
            )

            sindex = qt_frame.sindex
            try:
                # RTree syntax
                near_clusters = np.array(
                    list(sindex.nearest(bounds, num_results=num_results))
                )
            except TypeError:
                # PyGEOS syntax
                near_clusters = np.array(
                    list(sindex.nearest(box(*bounds), return_all=True))
                )[:num_results].flatten()

            # Duplicate the near grids
            qt_frame = pd.concat(
                (qt_frame, qt_frame.iloc[near_clusters]), axis=0
            )

        return qt_frame

    def weight_upsample(
        self, grid_df: gpd.GeoDataFrame, weight_method: str
    ) -> gpd.GeoDataFrame:
        # Add quadrant counts
        grid_df = grid_df.merge(
            (
                pd.DataFrame(self.counts, index=['qcounts'])
                .T.rename_axis(index=ID_COLUMN)
                .reset_index()
            ),
            on=ID_COLUMN,
        )
        oversample = np.array(1.0 / (grid_df.qcounts / grid_df.qcounts.max()))
        over_df: T.Sequence[pd.DataFrame] = []
        for over_val in np.unique(oversample):
            if weight_method == 'inverse-density':
                if over_val > 1:
                    repeated_index = grid_df.index[
                        np.where(oversample == over_val)
                    ].repeat(int(over_val))
                    over_df.append(grid_df.loc[repeated_index])
            else:
                if over_val > 2:
                    repeated_index = grid_df.index[
                        np.where(oversample == over_val)
                    ].repeat(1)
                    over_df.append(grid_df.loc[repeated_index])

        if over_df:
            grid_df = pd.concat((grid_df, pd.concat(over_df)))
            grid_df = grid_df.sort_values(by=ID_COLUMN)

        return grid_df

    def sample(
        self,
        n: int = 1,
        samples_per_grid: int = 1,
        strata_column: T.Optional[str] = None,
        weight_method: T.Optional[str] = None,
        weight_sample_by_distance: bool = False,
        multiply_distance_weights_by: float = 1.0,
        random_state: T.Optional[int] = None,
        rng: T.Optional[np.random.Generator] = None,
        **kwargs,
    ):
        """Samples from the hierarchical grid address using the Generalized
        Random Tessellation Stratified (GRTS) method.

        Args:
            n (int): The target grid sample size (i.e., the number of grids).
            samples_per_grid (int): The number of samples per grid.
            strata_column (Optional[str]): A columne to stratify samples by.
            weight_method (Optional[str]): The grid weight method.
                Choices are ['density-factor', 'inverse-density', 'cluster'].
            weight_sample_by_distance (bool): Whether to weight samples by distance from grid edge.
            multiply_distance_weights_by (float): A multiplicative value to apply to distance weights.
            random_state (Optional[int]): A random state for the random number generator.
            kwargs (Optional[dict]): Keyword arguments for ``self.weight_grids``.

        Returns:
            ``geopandas.GeoDataFrame``
        """
        if rng is None:
            rng = np.random.default_rng(random_state)

        if weight_method == 'cluster':
            grid_df = self.weight_grids(**kwargs)
        else:
            grid_df = self.to_frame()

        # Base 4 reverse sorting
        grid_df = grid_df.sort_values(by=ID_COLUMN)
        if weight_method in (
            'density-factor',
            'inverse-density',
        ):
            self.weight_upsample(grid_df, weight_method)

        npool = len(grid_df.index)
        df_sample = grid_df.iloc[:: int(np.ceil(npool / n))]

        if n > 0.5 * len(grid_df.index):
            df_sample = pd.concat(
                (
                    df_sample,
                    grid_df.query(
                        f"uid != {df_sample[ID_COLUMN].values.tolist()}"
                    ).sample(
                        n=n - len(df_sample.index),
                        random_state=rng.integers(low=0, high=100_000),
                    ),
                ),
            )

        sample_indices: T.List[int] = []
        # Iterate over the selected grids,
        # get intersecting samples, and
        # select 1 sample within each grid.
        for row_geometry, qsamples in self.iter_samples(df_sample):
            rng.shuffle(qsamples)

            if strata_column is not None:
                qdf = pd.DataFrame(
                    data=np.c_[
                        qsamples, self.dataframe.iloc[qsamples][strata_column]
                    ],
                    columns=['sample_index', strata_column],
                )
                qsamples = (
                    qdf.groupby(strata_column, group_keys=False).apply(
                        lambda x: x.sample(
                            min(samples_per_grid, len(x.index)),
                            replace=False,
                            random_state=rng.integers(low=0, high=100_000),
                        )
                    )
                ).sample_index.tolist()
            elif weight_sample_by_distance:
                qdf = pd.DataFrame(
                    data=qsamples,
                    columns=['sample_index'],
                )
                distance_weights = row_geometry.exterior.distance(
                    self.dataframe.iloc[qsamples].geometry
                )
                distance_weights = (
                    1.0 - (distance_weights / distance_weights.max())
                ).clip(0.1, 1)
                distance_weights *= multiply_distance_weights_by
                distance_weights /= distance_weights.sum()

                qsamples = qdf.sample(
                    n=samples_per_grid,
                    replace=False,
                    weights=distance_weights.values,
                    random_state=rng.integers(low=0, high=100_000),
                ).sample_index.tolist()
            else:
                qsamples = qsamples[:samples_per_grid]

            sample_indices.extend(qsamples)

        sample_indices = np.array(
            sorted(list(set(sample_indices))),
            dtype='int64',
        )

        # Get the random points
        return self.dataframe.iloc[sample_indices]


class Rtree(TreeMixin):
    def __init__(self, dataframe):
        super(Rtree, self).__init__(dataframe)

    def __len__(self):
        for group_idx, indices, bbox in self.sindex.leaves():
            n = len(indices)
            break

        return n

    @property
    def nleaves(self):
        return len(self.sindex.leaves())

    def to_geom(self):
        """Converts leaves to geometry."""
        return [
            self.create_poly(bbox)
            for group_idx, indices, bbox in self.sindex.leaves()
        ]

    def to_frame(self):
        """Converts leaves to a DataFrame."""
        return gpd.GeoDataFrame(
            data=range(0, self.nleaves),
            geometry=self.to_geom(),
            crs=self.crs,
            columns=[ID_COLUMN],
        )