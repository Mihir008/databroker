import collections.abc
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import dask.array
import h5py
import numpy
import pydantic
import pymongo
from fastapi import APIRouter, HTTPException, Request, Security
from tiled.adapters.array import ArrayAdapter
from tiled.adapters.utils import IndexersMixin, tree_repr
from tiled.server.core import json_or_msgpack
from tiled.server.dependencies import entry
from tiled.structures.array import ArrayStructure
from tiled.query_registration import QueryTranslationRegistry

from dataclasses import asdict
from tiled.structures.core import StructureFamily
from apischema import deserialize 

# from tiled.structures.array import ArrayStructure
from tiled.utils import UNCHANGED

from .schemas import Document

class PostMetadataRequest(pydantic.BaseModel):
    structure_family: StructureFamily
    structure: Any  # TODO ArrayStructure
    metadata: Dict    
    specs: List[str]
    mimetype: str


class PostMetadataResponse(pydantic.BaseModel):
    uid: str


router = APIRouter()


@router.post("/node/metadata/{path:path}", response_model=PostMetadataResponse)
def post_metadata(
    request: Request,
    body: PostMetadataRequest,
    entry=Security(entry, scopes=["write:data", "write:metadata"]),
):
    if not isinstance(entry, MongoAdapter):
        raise HTTPException(
            status_code=404, detail="This path cannot accept reconstruction metadata."
        )
    uid = entry.post_metadata(metadata=body.metadata, structure_family=body.structure_family,
                              structure=ArrayStructure(macro=body.structure["macro"], micro=body.structure["micro"]),
                              specs=body.specs, mimetype=body.mimetype)
    return json_or_msgpack(request, {"uid": uid})


@router.put("/array/full/{path:path}")
async def put_array_full(
    request: Request,
    entry=Security(entry, scopes=["write:data", "write:metadata"]),
):
    if not isinstance(entry, ReconAdapter):
        raise HTTPException(
            status_code=404, detail="This path cannot accept reconstruction data."
        )
    data = await request.body()
    entry.put_data(data)


def raise_if_inactive(method):
    def inner(self, *args, **kwargs):
        if self.array_adapter is None:
            raise ValueError("Not active")
        else:
            return method(self, *args, **kwargs)

    return inner


class ReconAdapter:
    structure_family = "array"

    def __init__(self, collection, directory, doc):
        self.collection = collection
        self.directory = directory
        self.doc = Document(**doc)
        self.array_adapter = None
        if self.doc.active:
            file = h5py.File(self.doc.path)
            dataset = file["data"]
            self.array_adapter = ArrayAdapter(dask.array.from_array(dataset))

    @property
    def structure(self):
        return ArrayStructure.from_json(self.doc.structure)

    @property
    def metadata(self):
        return self.doc.metadata

    @raise_if_inactive
    def read(self, *args, **kwargs):
        return self.array_adapter.read(*args, **kwargs)

    @raise_if_inactive
    def read_block(self, *args, **kwargs):
        return self.array_adapter.read_block(*args, **kwargs)

    def microstructure(self):
        return self.array_adapter.microstructure()

    def macrostructure(self):
        return self.array_adapter.macrostructure()

    def put_data(self, body):
        # Organize files into subdirectories with the first two
        # charcters of the uid to avoid one giant directory.
        path = self.directory / self.doc.uid[:2] / self.doc.uid
        path.parent.mkdir(parents=True, exist_ok=True)
        array = numpy.frombuffer(
            body, dtype=self.structure.micro.to_numpy_dtype()
        ).reshape(self.structure.macro.shape)
        with h5py.File(path, "w") as file:
            file.create_dataset("data", data=array)
        self.collection.update_one(
            {"uid": self.doc.uid},
            {"$set": {"data_url": "file://" + str(path)}},
        )


class MongoAdapter(collections.abc.Mapping, IndexersMixin):
    structure_family = "node"
    include_routers = [router]
    query_registry = QueryTranslationRegistry()
    register_query = query_registry.register

    def __init__(
        self,
        *,
        database,
        directory,
        queries=None,
        sorting=None,
        metadata=None,
        principal=None,
        access_policy=None,
    ):
        self.database = database
        self.collection = database["reconstructions"]
        self.directory = Path(directory).resolve()
        if not self.directory.exists():
            raise ValueError(f"Directory {self.directory} does not exist.")
        if not self.directory.is_dir():
            raise ValueError(
                f"The given directory path {self.directory} is not a directory."
            )
        if not os.access(self.directory, os.W_OK):
            raise ValueError("Directory {self.directory} is not writeable.")
        self.queries = queries or []
        self.sorting = sorting or [("metadata.scan_id", 1)]
        self.metadata = metadata or {}
        self.principal = principal
        self.access_policy = access_policy
        super().__init__()

    @classmethod
    def from_uri(cls, uri, directory, *, metadata=None):
        if not pymongo.uri_parser.parse_uri(uri)["database"]:
            raise ValueError(
                f"Invalid URI: {uri!r} " f"Did you forget to include a database?"
            )
        client = pymongo.MongoClient(uri)
        database = client.get_database()
        return cls(database=database, directory=directory, metadata=metadata)

    def new_variation(
        self,
        metadata=UNCHANGED,
        queries=UNCHANGED,
        sorting=UNCHANGED,
        principal=UNCHANGED,
        **kwargs,
    ):
        if metadata is UNCHANGED:
            metadata = self.metadata
        if queries is UNCHANGED:
            queries = self.queries
        if sorting is UNCHANGED:
            sorting = self.sorting
        if principal is UNCHANGED:
            principal = self.principal
        return type(self)(
            database=self.database,
            directory=self.directory,
            metadata=metadata,
            queries=queries,
            sorting=sorting,
            access_policy=self.access_policy,
            principal=principal,
            **kwargs,
        )

    def post_metadata(self, metadata, structure_family, structure, specs, mimetype):
        uid = str(uuid.uuid4())
        document = {
            "id": uid,
            "structure_family": structure_family,
            "structure": structure,
            "metadata": metadata,
            "specs": specs,
            "mimetype": mimetype,
        }
        validated_document = deserialize(Document, document)
        self.collection.insert_one(
            asdict(validated_document)
        )
        return uid

    def authenticated_as(self, identity):
        if self.principal is not None:
            raise RuntimeError(f"Already authenticated as {self.principal}")
        if self.access_policy is not None:
            raise NotImplementedError("No support for Access Policy")
        return self

    def _build_mongo_query(self, *queries):
        combined = self.queries + list(queries)
        if combined:
            return {"$and": combined}
        else:
            return {}

    def __getitem__(self, key):
        query = {"uid": key}
        doc = self.collection.find_one(self._build_mongo_query(query), {"_id": False})
        if doc is None:
            raise KeyError(key)
        return ReconAdapter(self.collection, self.directory, doc)

    def __iter__(self):
        # TODO Apply pagination, as we do in Databroker.
        for doc in list(
            self.collection.find(
                self._build_mongo_query({"active": True}), {"uid": True}
            )
        ):
            yield doc["uid"]

    def __len__(self):
        return self.collection.count_documents(
            self._build_mongo_query({"active": True})
        )

    def __length_hint__(self):
        # https://www.python.org/dev/peps/pep-0424/
        return self.collection.estimated_document_count(
            self._build_mongo_query({"active": True}),
        )

    def __repr__(self):
        # Display up to the first N keys to avoid making a giant service
        # request. Use _keys_slicer because it is unauthenticated.
        N = 10
        return tree_repr(self, self._keys_slice(0, N, direction=1))

    def search(self, query):
        """
        Return a MongoAdapter with a subset of the mapping.
        """
        return self.query_registry(query, self)

    def sort(self, sorting):
        return self.new_variation(sorting=sorting)

    def _keys_slice(self, start, stop, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        skip = start or 0
        if stop is not None:
            limit = stop - skip
        else:
            limit = None
        for doc in self.collection.find(
            self._build_mongo_query({"active": True}),
            skip=skip,
            limit=limit,
        ):
            yield doc["uid"]

    def _items_slice(self, start, stop, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        skip = start or 0
        if stop is not None:
            limit = stop - skip
        else:
            limit = None
        for doc in self.collection.find(
            self._build_mongo_query({"active": True}),
            skip=skip,
            limit=limit,
        ):
            yield (doc["uid"], ReconAdapter(self.database, self.directory, doc))

    def _item_by_index(self, index, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        doc = next(
            self.collection.find(
                self._build_mongo_query({"active": True}),
                skip=index,
                limit=1,
            )
        )
        return (doc["uid"], ReconAdapter(self.database, self.directory, doc))


# def raw_mongo(query, catalog):
#     # For now, only handle search on the 'run_start' collection.
#     return catalog.new_variation(
#         queries=catalog.queries + [json.loads(query.query)],
#     )


# MongoAdapter.register_query(RawMongo, raw_mongo)
