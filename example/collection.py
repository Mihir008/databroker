__author__ = 'arkilic'


from filestore.api.collection import insert_nugget, insert_resource, insert_resourse_attributes


base = insert_resource(spec='some spec', file_path='/tmp/filestore/dummy/file/path', custom={'some_info': 'info'})

print base.id, base.file_path, base.spec

insert_resourse_attributes(resource=base, dtype='float32', shape='1000x1000')



insert_nugget(resource=base, event_id='54b59cf5fa44833081ba8282')
# Note:This is not a real id inside metadataStore.No need to blame Arman for referring to non-existent documents
