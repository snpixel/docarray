import uuid
from collections import defaultdict
from typing import (
    TypeVar,
    Generic,
    Optional,
    List,
    Dict,
    Any,
    Sequence,
    Union,
    Generator,
    Type,
    cast,
    TYPE_CHECKING,
    Iterator,
    Mapping,
    Tuple,
)
from dataclasses import dataclass, field

import json
import numpy as np
from numpy import ndarray

from docarray.index.backends.helper import _collect_query_args
from docarray import BaseDoc, DocList
from docarray.index.abstract import (
    BaseDocIndex,
    _raise_not_composable,
)
from docarray.typing import NdArray
from docarray.typing.tensor.abstract_tensor import AbstractTensor
from docarray.utils._internal.misc import import_library
from docarray.utils.find import _FindResultBatched, _FindResult, FindResult

if TYPE_CHECKING:
    import redis
    from redis.commands.search.query import Query
    from redis.commands.search.field import (  # type: ignore[import]
        NumericField,
        TextField,
        VectorField,
    )
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType  # type: ignore[import]
else:
    redis = import_library('redis')

    from redis.commands.search.field import (
        NumericField,
        TextField,
        VectorField,
    )
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
    from redis.commands.search.query import Query

TSchema = TypeVar('TSchema', bound=BaseDoc)

VALID_DISTANCES = ['L2', 'IP', 'COSINE']
VALID_ALGORITHMS = ['FLAT', 'HNSW']
VALID_TEXT_SCORERS = [
    'BM25',
    'TFIDF',
    'TFIDF.DOCNORM',
    'DISMAX',
    'DOCSCORE',
    'HAMMING',
]


class RedisDocumentIndex(BaseDocIndex, Generic[TSchema]):
    def __init__(self, db_config=None, **kwargs):
        """Initialize RedisDocumentIndex"""
        super().__init__(db_config=db_config, **kwargs)
        self._db_config = cast(RedisDocumentIndex.DBConfig, self._db_config)

        if not self._db_config.index_name:
            self._db_config.index_name = 'index_name__' + self._random_name()
        self._prefix = self._db_config.index_name + ':'  # type: ignore[operator]

        # initialize Redis client
        self._client = redis.Redis(
            host=self._db_config.host,
            port=self._db_config.port,
            username=self._db_config.username,
            password=self._db_config.password,
            decode_responses=False,
        )
        self._create_index()
        self._logger.info(f'{self.__class__.__name__} has been initialized')

    @staticmethod
    def _random_name() -> str:
        """Generate a random index name."""
        return uuid.uuid4().hex

    def _create_index(self) -> None:
        """Create a new index in the Redis database if it doesn't already exist."""
        if not self._check_index_exists(self._db_config.index_name):  # type: ignore[arg-type]
            schema = []
            for column, info in self._column_infos.items():
                if info.db_type == VectorField:
                    space = info.config.get('space') or info.config.get('distance')
                    space = space.upper()  # type: ignore[union-attr]
                    if space not in VALID_DISTANCES:
                        raise ValueError(
                            f"Invalid distance metric '{space}' provided. "
                            f"Must be one of: {', '.join(VALID_DISTANCES)}"
                        )
                    attributes = {
                        'TYPE': 'FLOAT32',
                        'DIM': info.n_dim or info.config.get('dim'),
                        'DISTANCE_METRIC': space,
                        'EF_CONSTRUCTION': info.config['ef_construction'],
                        'EF_RUNTIME': info.config['ef_runtime'],
                        'M': info.config['m'],
                        'INITIAL_CAP': info.config['initial_cap'],
                    }
                    attributes = {
                        name: value for name, value in attributes.items() if value
                    }
                    algorithm = info.config['algorithm'].upper()
                    if algorithm not in VALID_ALGORITHMS:
                        raise ValueError(
                            f"Invalid algorithm '{algorithm}' provided. "
                            f"Must be one of: {', '.join(VALID_ALGORITHMS)}"
                        )
                    schema.append(
                        info.db_type(
                            '$.' + column,
                            algorithm=algorithm,
                            attributes=attributes,
                            as_name=column,
                        )
                    )
                else:
                    schema.append(info.db_type('$.' + column, as_name=column))

            # Create Redis Index
            self._client.ft(self._db_config.index_name).create_index(  # type: ignore[arg-type]
                schema,
                definition=IndexDefinition(
                    prefix=[self._prefix], index_type=IndexType.JSON
                ),
            )

            self._logger.info(f'index {self._db_config.index_name} has been created')
        else:
            self._logger.info(
                f'connected to existing {self._db_config.index_name} index'
            )

    def _check_index_exists(self, index_name: str) -> bool:
        """
        Check if an index exists in the Redis database.

        :param index_name: The name of the index.
        :return: True if the index exists, False otherwise.
        """
        try:
            self._client.ft(index_name).info()
        except:  # noqa: E722
            self._logger.info(f'Index {index_name} does not exist')
            return False
        self._logger.info(f'Index {index_name} already exists')
        return True

    class QueryBuilder(BaseDocIndex.QueryBuilder):
        def __init__(self, query: Optional[List[Tuple[str, Dict]]] = None):
            super().__init__()
            # list of tuples (method name, kwargs)
            self._queries: List[Tuple[str, Dict]] = query or []

        def build(self, *args, **kwargs) -> Any:
            """Build the query object."""
            return self._queries

        find = _collect_query_args('find')
        filter = _collect_query_args('filter')
        text_search = _raise_not_composable('text_search')
        find_batched = _raise_not_composable('find_batched')
        filter_batched = _raise_not_composable('filter_batched')
        text_search_batched = _raise_not_composable('text_search_batched')

    @dataclass
    class DBConfig(BaseDocIndex.DBConfig):
        """Dataclass that contains all "static" configurations of RedisDocumentIndex.

        :param host: The host address for the Redis server. Default is 'localhost'.
        :param port: The port number for the Redis server. Default is 6379.
        :param index_name: The name of the index in the Redis database.
            In case it's not provided, a random index name will be generated.
        :param username: The username for the Redis server. Default is None.
        :param password: The password for the Redis server. Default is None.
        :param text_scorer: The method for scoring text during text search.
            Default is 'BM25'.
        :param default_column_config: Default configuration for columns.
        """

        host: str = 'localhost'
        port: int = 6379
        index_name: Optional[str] = None
        username: Optional[str] = None
        password: Optional[str] = None
        text_scorer: str = field(default='BM25')
        default_column_config: Dict[Type, Dict[str, Any]] = field(
            default_factory=lambda: defaultdict(
                dict,
                {
                    VectorField: {
                        'algorithm': 'FLAT',
                        'distance': 'COSINE',
                        'ef_construction': None,
                        'm': None,
                        'ef_runtime': None,
                        'initial_cap': None,
                    },
                },
            )
        )

        def __post_init__(self):
            self.text_scorer = self.text_scorer.upper()

            if self.text_scorer not in VALID_TEXT_SCORERS:
                raise ValueError(
                    f"Invalid text scorer '{self.text_scorer}' provided. "
                    f"Must be one of: {', '.join(VALID_TEXT_SCORERS)}"
                )

    @dataclass
    class RuntimeConfig(BaseDocIndex.RuntimeConfig):
        """Dataclass that contains all "dynamic" configurations of RedisDocumentIndex."""

        default_column_config: Dict[Any, Dict[str, Any]] = field(
            default_factory=lambda: {
                TextField: {},
                NumericField: {},
                VectorField: {},
            }
        )

    def python_type_to_db_type(self, python_type: Type) -> Any:
        """
        Map python types to corresponding Redis types.

        :param python_type: Python type.
        :return: Corresponding Redis type.
        """
        type_map = {
            int: NumericField,
            float: NumericField,
            str: TextField,
            bytes: TextField,
            np.ndarray: VectorField,
            list: VectorField,
            AbstractTensor: VectorField,
        }

        for py_type, redis_type in type_map.items():
            if issubclass(python_type, py_type):
                return redis_type
        raise ValueError(f'Unsupported column type for {type(self)}: {python_type}')

    @staticmethod
    def _generate_item(
        column_to_data: Dict[str, Generator[Any, None, None]]
    ) -> Iterator[Dict[str, Any]]:
        """
        Given a dictionary of generators, yield a dictionary where each item consists of a key and
        a single item from the corresponding generator.

        :param column_to_data: A dictionary where each key is a column and each value
            is a generator.

        :yield: A dictionary where each item consists of a column name and an item from
            the corresponding generator. Yields until all generators are exhausted.
        """
        keys = list(column_to_data.keys())
        iterators = [iter(column_to_data[key]) for key in keys]
        while True:
            item_dict = {}
            for key, it in zip(keys, iterators):
                item = next(it, None)

                if key == 'id' and not item:
                    return

                if isinstance(item, AbstractTensor):
                    item_dict[key] = item._docarray_to_ndarray().tolist()
                elif isinstance(item, ndarray):
                    item_dict[key] = item.astype(np.float32).tolist()
                elif item is not None:
                    item_dict[key] = item

            yield item_dict

    def _index(
        self, column_to_data: Dict[str, Generator[Any, None, None]]
    ) -> List[str]:
        """
        Indexes the given data into Redis.

        :param column_to_data: A dictionary where each key is a column and each value is a generator.
        :return: A list of document ids that have been indexed.
        """
        ids = []
        for item in self._generate_item(column_to_data):
            ids.append(item['id'])
            doc_id = self._prefix + item['id']
            self._client.json().set(doc_id, '$', item)
        return ids

    def num_docs(self) -> int:
        """
        Fetch the number of documents in the index.

        :return: Number of documents in the index.
        """
        num_docs = self._client.ft(self._db_config.index_name).info()['num_docs']  # type: ignore[arg-type]
        return int(num_docs)

    def _del_items(self, doc_ids: Sequence[str]) -> None:
        """
        Deletes documents from the index based on document ids.

        :param doc_ids: A sequence of document ids to be deleted.
        """
        doc_ids = [self._prefix + id for id in doc_ids if self._doc_exists(id)]
        if doc_ids:
            self._client.delete(*doc_ids)

    def _doc_exists(self, doc_id) -> bool:
        """
        Checks if a document exists in the index.

        :param doc_id: The id of the document.
        :return: True if the document exists, False otherwise.
        """
        return bool(self._client.exists(self._prefix + doc_id))

    def _get_items(
        self, doc_ids: Sequence[str]
    ) -> Union[Sequence[TSchema], Sequence[Dict[str, Any]]]:
        """
        Fetches the documents from the index based on document ids.

        :param doc_ids: A sequence of document ids.
        :return: A sequence of documents from the index.
        """
        if not doc_ids:
            return []

        docs = []
        for id in doc_ids:
            doc = self._client.json().get(self._prefix + id)
            if doc:
                docs.append(doc)

        if len(docs) == 0:
            raise KeyError(f'No document with id {doc_ids} found')
        return docs

    def execute_query(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        """
        Executes a hybrid query on the index.

        :param query: Query to execute on the index.
        :return: Query results.
        """
        components: Dict[str, List[Dict[str, Any]]] = {}
        for component, value in query:
            if component not in components:
                components[component] = []
            components[component].append(value)

        if (
            len(components) != 2
            or len(components.get('find', [])) != 1
            or len(components.get('filter', [])) != 1
        ):
            raise ValueError(
                'The query must contain exactly one "find" and "filter" components.'
            )

        filter_query = components['filter'][0]['filter_query']
        query = components['find'][0]['query']
        search_field = components['find'][0]['search_field']
        limit = (
            components['find'][0].get('limit')
            or components['filter'][0].get('limit')
            or 10
        )
        docs, scores = self._hybrid_search(
            query=query,
            filter_query=filter_query,
            search_field=search_field,
            limit=limit,
        )
        docs = self._dict_list_to_docarray(docs)
        return FindResult(documents=docs, scores=scores)

    def _hybrid_search(
        self, query: np.ndarray, filter_query: str, search_field: str, limit: int
    ) -> _FindResult:
        """
        Conducts a hybrid search (a combination of vector search and filter-based search) on the index.

        :param query: The query to search.
        :param filter_query: The filter condition.
        :param search_field: The vector field to search on.
        :param limit: The maximum number of results to return.
        :return: Query results.
        """
        redis_query = (
            Query(f'{filter_query}=>[KNN {limit} @{search_field} $vec AS vector_score]')
            .sort_by('vector_score')
            .paging(0, limit)
            .dialect(2)
        )
        query_params: Mapping[str, str] = {  # type: ignore
            'vec': np.array(query, dtype=np.float32).tobytes()  # type: ignore
        }
        results = (
            self._client.ft(self._db_config.index_name)  # type: ignore[arg-type]
            .search(redis_query, query_params)
            .docs
        )

        scores: NdArray = NdArray._docarray_from_native(
            np.array([document['vector_score'] for document in results])
        )

        docs = []
        for out_doc in results:
            doc_dict = json.loads(out_doc.json)
            docs.append(doc_dict)
        return _FindResult(documents=docs, scores=scores)

    def _find(
        self, query: np.ndarray, limit: int, search_field: str = ''
    ) -> _FindResult:
        """
        Conducts a search on the index.

        :param query: The vector query to search.
        :param limit: The maximum number of results to return.
        :param search_field: The field to search the query.
        :return: Search results.
        """
        return self._hybrid_search(
            query=query, filter_query='*', search_field=search_field, limit=limit
        )

    def _find_batched(
        self, queries: np.ndarray, limit: int, search_field: str = ''
    ) -> _FindResultBatched:
        """
        Conducts a batched search on the index.

        :param queries: The queries to search.
        :param limit: The maximum number of results to return for each query.
        :param search_field: The field to search the queries.
        :return: Search results.
        """
        docs, scores = [], []
        for query in queries:
            results = self._find(query=query, search_field=search_field, limit=limit)
            docs.append(results.documents)
            scores.append(results.scores)

        return _FindResultBatched(documents=docs, scores=scores)

    def _filter(self, filter_query: Any, limit: int) -> Union[DocList, List[Dict]]:
        """
        Filters the index based on the given filter query.

        :param filter_query: The filter condition.
        :param limit: The maximum number of results to return.
        :return: Filter results.
        """
        q = Query(filter_query)
        q.paging(0, limit)

        results = self._client.ft(index_name=self._db_config.index_name).search(q).docs  # type: ignore[arg-type]
        docs = [json.loads(doc.json) for doc in results]
        return docs

    def _filter_batched(
        self, filter_queries: Any, limit: int
    ) -> Union[List[DocList], List[List[Dict]]]:
        """
        Filters the index based on the given batch of filter queries.

        :param filter_queries: The filter conditions.
        :param limit: The maximum number of results to return for each filter query.
        :return: Filter results.
        """
        results = []
        for query in filter_queries:
            results.append(self._filter(filter_query=query, limit=limit))
        return results

    def _text_search(
        self, query: str, limit: int, search_field: str = ''
    ) -> _FindResult:
        """
        Conducts a text-based search on the index.

        :param query: The query to search.
        :param limit: The maximum number of results to return.
        :param search_field: The field to search the query.
        :return: Search results.
        """
        query_str = '|'.join(query.split(' '))
        q = (
            Query(f'@{search_field}:{query_str}')
            .scorer(self._db_config.text_scorer)  # type: ignore[union-attr]
            .with_scores()
            .paging(0, limit)
        )

        results = self._client.ft(index_name=self._db_config.index_name).search(q).docs  # type: ignore[arg-type]

        scores: NdArray = NdArray._docarray_from_native(
            np.array([document['score'] for document in results])
        )

        docs = [json.loads(doc.json) for doc in results]

        return _FindResult(documents=docs, scores=scores)

    def _text_search_batched(
        self, queries: Sequence[str], limit: int, search_field: str = ''
    ) -> _FindResultBatched:
        """
        Conducts a batched text-based search on the index.

        :param queries: The queries to search.
        :param limit: The maximum number of results to return for each query.
        :param search_field: The field to search the queries.
        :return: Search results.
        """
        docs, scores = [], []
        for query in queries:
            results = self._text_search(
                query=query, search_field=search_field, limit=limit
            )
            docs.append(results.documents)
            scores.append(results.scores)

        return _FindResultBatched(documents=docs, scores=scores)
