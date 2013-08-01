import requests
import json
import os
import re
import urllib
import base64
import hashlib
import sys
import cStringIO
import StringIO
from multiprocessing import Pool
import logging

from django.conf import settings
from pymongo import MongoClient
from pdfminer.pdfinterp import PDFResourceManager, process_pdf
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfparser import PDFSyntaxError
from wand.image import Image
from wand.exceptions import DelegateError, MissingDelegateError, CorruptImageError
from xhtml2pdf import pisa as pisa

log = logging.getLogger("mitx.courseware")
MONGO_COURSE_CACHE = {}

class ElasticDatabase:
    """
    This is a wrapper class for ElasticSearch, implemented through ElasticSearch's REST api and requests.

    In a very broad sense there are two layers of nesting in elasticsearch storage. The top level is
    an index. In this implementation indicies correspond to types of content (transcript, problem, etc...).
    The second level is a type. In this implementation a type is a course_id. Currently because elasticsearch
    doesn't like having slashes in type names, types are SHA1 hashes of the course id instead of the course
    id itself. 

    In addition to those two levels of nesting, each individual piece of data has an id associated with it.
    Currently the id of each object is a SHA1 hash of its entire id field. 

    Each index has a setup associated with it. These settings are quite minimal, they just specify how many
    nodes/shards we would like to have an index distributed across. 

    Each type has a mapping associated with it. A mapping is essentially a database schema with some additional
    information surrounding search functionality. For instance, which tokenizers and analyzers to use.

    Right now these settings are entirely specified through JSON in the settings.json file located within this
    directory. Most of the basic methods in this class serve to properly instantiate types and indices,
    in addition to running some basic queries and indexing content.  
    """

    def __init__(self):
        """
        Instantiates the ElasticDatabase file.

        This includes a url, which should point to the location of the elasticsearch server.
        The only other input here is the settings file, which should also be specified
        within the settings file.
        """

        self.url = "http://localhost:9200"  # settings.ES_DATABASE
        self.index_settings = json.loads(open(settings., 'rb').read())

    def setup_type(self, index, type_, json_mapping):
        """
        Instantiates a type within the Elastic Search instance
        
        json_mapping should be a dictionary starting at the properties level of a mapping.

        The type level will be added, so if you include it things will break. The purpose of this
        is to encourage loose coupling between types and mappings for better code
        """

        full_url = "/".join([self.url, index, type_]) + "/"
        dictionary = json.loads(open(json_mapping).read())
        return requests.post(full_url, data=json.dumps(dictionary))

    def has_index(self, index):
        """
        Checks to see if a given index exists in the database returns existance boolean,

        If this returns something other than a 200 or a 404 something is wrong and so we error
        """

        full_url = "/".join([self.url, index])
        status = requests.head(full_url).status_code
        if status == 200:
            return True
        elif status == 404:
            return False
        else:
            log.debug("Got an unexpected reponse code: " + str(status) + " at: " + full_url)
            raise

    def has_type(self, index, type_):
        """
        Same as has_index method, but for a given type
        """

        full_url = "/".join([self.url, index, type_])
        status = requests.head(full_url).status_code
        if status == 200:
            return True
        elif status == 404:
            return False
        else:
            log.debug("Got an unexpected reponse code: " + str(status) + " at: " + full_url)
            raise

    def index_directory_files(self, directory, index, type_, silent=False, **kwargs):
        """
        Starts a pygrep instance and indexes all files in the given directory.

        Available kwargs are file_ending, callback, and conserve_kwargs.
        Respectively these allow you to choose the file ending to be indexed, the
        callback used to do the indexing, and whether or not you would like to pass
        your kwargs to the callback function
        """

        # Needs to be lazily evaluatedy
        file_ending = kwargs.get("file_ending", ".srt.sjson")
        callback = kwargs.get("callback", self.index_transcript)
        conserve_kwargs = kwargs.get("conserve_kwargs", False)
        directoryCrawler = PyGrep(directory)
        all_files = directoryCrawler.grab_all_files_with_ending(file_ending)
        responses = []
        for file_list in all_files:
            for file_ in file_list:
                if conserve_kwargs:
                    responses.append(callback(index, type_, file_, silent, **kwargs))
                else:
                    responses.append(callback(index, type_, file_, silent))
        return responses

    def index_transcript(self, index, type_, transcript_file, silent=False, id_=None):
        """
        Indexes the given transcript file at the given index, type, and id
        """

        file_uuid = transcript_file.rsplit("/")[-1][:-10]
        transcript = open(transcript_file, 'rb').read()
        try:
            searchable_text = " ".join(filter(None, json.loads(transcript)["text"])).replace("\n", " ")
        except ValueError:
            if silent:
                searchable_text = "INVALID JSON"
            else:
                raise
        data = {"searchable_text": searchable_text, "uuid": file_uuid}
        if not id_:
            return self.index_data(index, type_, data)._content
        else:
            return self.index_data(index, type_, data, id_=id_)
        return self.index_data(index, type_, id_, data)._content

    def setup_index(self, index):
        """
        Creates a new elasticsearch index, returns the response it gets
        """

        full_url = "/".join([self.url, index]) + "/"
        return requests.put(full_url, data=json.dumps(self.index_settings))

    def add_index_settings(self, index, index_settings=None):
        """
        Adds a settings file to the given index
        """

        index_settings = index_settings or self.index_settings
        full_url = "/".join([self.url, index]) + "/"
        #closing the index so it can be changed
        requests.post(full_url+"/_close")
        response = requests.post(full_url+"/", data=json.dumps(index_settings))
        #reopening the index so it can be read
        requests.post(full_url+"/_open")
        return response

    def index_data(self, index, data, type_=None, id_=None):
        """
        Actually indexes given data at the indicated type and id.

        If no type or id is provided, this will assume that the type and id are
        contained within the data object passed to the index_data function in the
        hash and type_hash fields.

        Data should be a dictionary that matches the mapping of the given type.
        """

        if id_ is None:
            id_ = data["hash"]
        if type_ is None:
            type_ = data["type_hash"]
        full_url = "/".join([self.url, index, type_, id_])
        response = requests.post(full_url, json.dumps(data))
        return response

    def bulk_index(self, all_data):
        """
        Allows for bulk indexing of properly formatted json strings.
        Example: 
        {"index": {"_index": "transcript-index", "_type": "course_hash", "_id": "id_hash"}}
        {"field1": "value1"...}

        Important: Bulk indexing is newline delimited, make sure the newlines are properly used
        """

        url = self.url + "/_bulk"
        response = requests.post(url, data=all_data)
        return response


    def get_data(self, index, type_, id_):
        """
        Returns the data located at a specific index, type and id within the elasticsearch instance
        """

        full_url = "/".join([self.url, index, type_, id_])
        return requests.get(full_url)

    def get_index_settings(self, index):
        """
        Returns the current settings of a given index
        """

        full_url = "/".join([self.url, index, "_settings"])
        return json.loads(requests.get(full_url)._content)

    def delete_index(self, index):
        """
        Deletes the index specified, along with all contained types and data
        """

        full_url = "/".join([self.url, index])
        return requests.delete(full_url)

    def delete_type(self, index, type_):
        """
        Same as delete_index, but for types
        """

        full_url = "/".join([self.url, index, type_])
        return requests.delete(full_url)

    def get_type_mapping(self, index, type_):
        """
        Return the mapping of the indicated type
        """

        full_url = "/".join([self.url, index, type_, "_mapping"])
        return json.loads(requests.get(full_url)._content)


class MongoIndexer:
    """
    This class is the connection point between Mongo and ElasticSearch.
    """

    def __init__(
        self, host='localhost', port=27017, content_database='xcontent', file_collection="fs.files",
        chunk_collection="fs.chunks", module_database='xmodule', module_collection='modulestore',
        es_instance=ElasticDatabase()
    ):
        self.host = host
        self.port = port
        self.client = MongoClient(host, port)
        self.content_db = self.client[content_database]
        self.module_db = self.client[module_database]
        try:
            self.content_db.collection_names().index(file_collection)
        except ValueError:
            log.debug("No collection named: " + file_collection)
            raise
        try:
            self.content_db.collection_names().index(chunk_collection)
        except ValueError:
            log.debug("No collection named: " + chunk_collection)
            raise
        try:
            self.module_db.collection_names().index(module_collection)
        except ValueError:
            log.debug("No collection named: " + module_collection)
            raise
        self.file_collection = self.content_db[file_collection]
        self.chunk_collection = self.content_db[chunk_collection]
        self.module_collection = self.module_db[module_collection]
        self.es_instance = es_instance

    def find_files_with_type(self, file_ending):
        """Returns a cursor for content files matching given type"""
        return self.file_collection.find({"filename": re.compile(".*?"+re.escape(file_ending))}, timeout=False)

    def find_chunks_with_type(self, file_ending):
        """Returns a chunk cursor for content files matching given type"""
        return self.chunk_collection.find({"files_id.name": re.compile(".*?"+re.escape(file_ending))}, timeout=False)

    def find_modules_by_category(self, category):
        """Returns a cursor for all xmodules matching given category"""
        return self.module_collection.find({"_id.category": category}, timeout=False)

    def find_categories_with_regex(self, category, regex):
        return self.module_collection.find({"_id.category": category, "definition.data": regex}, timeout=False)

    def find_asset_with_name(self, name):
        return self.chunk_collection.find_one({"files_id.category": "asset", "files_id.name": name}, timeout=False)

    def find_modules_for_course(self, course):
        return self.module_collection.find({"_id.course": course}, timeout=False)

    def find_transcript_for_video_module(self, video_module):
        data = video_module.get("definition", {"data": ""}).get("data", "")
        if isinstance(data, dict):  # For some reason there are nested versions
            data = data.get("data", "")
        if not isinstance(data, unicode):  # for example videos
            return [""]
        uuid = self.uuid_from_video_module(video_module)
        if uuid == False:
            return [""]
        name_pattern = re.compile(".*?"+uuid+".*?")
        chunk = self.chunk_collection.find_one({"files_id.name": name_pattern})
        if chunk == None:
            return [""]
        elif "com.apple.quar" in chunk["data"].decode('utf-8', "ignore"):
            # This seemingly arbitrary error check brought to you by apple. 
            # This is an obscure, barely documented occurance where apple broke tarballs
            # and decided to shove error messages into tar metadata which causes this.
            # https://discussions.apple.com/thread/3145071?start=0&tstart=0
            return [""]
        else:
            try:
                return " ".join(filter(None, json.loads(chunk["data"].decode('utf-8', "ignore"))["text"]))
            except ValueError:
                log.error("Transcript for: " + uuid + " is invalid")
                return chunk["data"].decode('utf-8', 'ignore')

    def pdf_to_text(self, mongo_element):
        onlyAscii = lambda s: "".join(c for c in s if ord(c) < 128)
        resource = PDFResourceManager()
        return_string = cStringIO.StringIO()
        params = LAParams()
        converter = TextConverter(resource, return_string, codec='utf-8', laparams=params)
        fake_file = StringIO.StringIO(mongo_element["data"].__str__())
        try:
            process_pdf(resource, converter, fake_file)
        except PDFSyntaxError:
            log.debug(mongo_element["files_id"]["name"] + " cannot be read, moving on.")
            return ""
        text_value = onlyAscii(return_string.getvalue()).replace("\n", " ")
        return text_value

    def searchable_text_from_problem_data(self, mongo_element):
        """The data field from the problem is in weird xml, which is good for functionality, but bad for search"""
        data = mongo_element["definition"]["data"]
        try:
            paragraphs = " ".join([text for text in re.findall("<p>(.*?)</p>", data) if text is not "Explanation"])
        except TypeError:
            paragraphs = ""
        cleaned_text = re.sub("\\(.*?\\)", "", paragraphs).replace("\\", "")
        remove_tags = re.sub("<[a-zA-Z0-9/\.\= \"_-]+>", "", cleaned_text)
        remove_repetitions = re.sub(r"(.)\1{4,}", "", remove_tags)
        return remove_repetitions

    def uuid_from_file_name(self, file_name):
        """Returns a youtube uuid given the filename of a transcript"""
        if "subs_" in file_name:
            file_name = file_name[5+file_name.find("subs_"):]
        elif file_name[:2] == "._":
            file_name = file_name[2:]
        return file_name[:file_name.find(".")]

    def thumbnail_from_video_module(self, video_module):
        data = video_module.get("definition", {"data": ""}).get("data", "")
        if "player.youku.com" in data:  # Some videos use the youku player
            url = "https://lh6.ggpht.com/8_h5j6hiFXdSl5atSJDf8bJBy85b3IlzNWeRzOqRurfNVI_oiEG-dB3C0vHRclOG8A=w170"
            image = urllib.urlopen(url)
            return base64.b64encode(image.read())
        uuid = self.uuid_from_video_module(video_module)
        if uuid == False:
            url = "http://img.youtube.com/vi/Tt9g2se1LcM/4.jpg"
            image = urllib.urlopen(url)
            return base64.b64encode(image.read())
        image = urllib.urlopen("http://img.youtube.com/vi/" + uuid + "/0.jpg")
        return base64.b64encode(image.read())

    def uuid_from_video_module(self, video_module):
        data = video_module.get("definition", {"data": ""}).get("data", "")
        if isinstance(data, dict):
            data = data.get("data", "")
        uuids = data.split(",")
        if len(uuids) == 1:  # Some videos are just left over demos without links
            return False
        # The colon is kind of a hack to make sure there will always be a second element since
        # some entries don't have anything for the second entry
        speed_map = {(entry+":").split(":")[0]: (entry+":").split(":")[1] for entry in uuids}
        try:
            uuid = [value for key, value in speed_map.items() if "1.0" in key][0]
        except IndexError:
            print data
            print uuids
            print speed_map
        return uuid

    def thumbnail_from_pdf(self, pdf):
        try:
            with Image(blob=pdf) as img:
                return base64.b64encode(img.make_blob('jpg'))
        except (DelegateError, MissingDelegateError, CorruptImageError):
            raise

    def thumbnail_from_html(self, html):
        pseudo_dest = cStringIO.StringIO()
        pisa.CreatePDF(StringIO.StringIO(html), pseudo_dest)
        return self.thumbnail_from_pdf(pseudo_dest.getvalue())

    def vertical_url_from_mongo_element(self, mongo_element):
        """Given a mongo element, returns the url after courseware"""
        name = lambda x: x["_id"]["name"]
        element_name = name(mongo_element)
        vertical = self.module_collection.find_one({"definition.children": re.compile(".*?"+element_name+".*?")})
        if vertical:
            index = next(i for i, entry in enumerate(vertical["definition"]["children"]) if element_name in entry)
            sequential = self.module_collection.find_one({
                "definition.children": re.compile(".*?"+name(vertical)+".*?"),
                "_id.course": vertical["_id"]["course"]
            })
            if sequential and sequential["_id"]:
                chapter = self.module_collection.find_one({
                    "definition.children": re.compile(".*?"+name(sequential)+".*?"),
                    "_id.course": sequential["_id"]["course"]
                })
                if chapter and chapter["_id"]:
                    return "/".join([name(chapter), name(sequential), str(index+1)])
                else:
                    print "No Chapter for: " + sequential["metadata"]["display_name"]
                    return None
            else:
                print "No Sequential for: " + vertical["metadata"]["display_name"]
                return None
        else:
            return None

    def course_name_from_mongo_module(self, mongo_module):
        course_element = self.module_collection.find_one({
            "_id.course": mongo_module["_id"]["course"],
            "_id.category": "course"
        })
        return course_element["_id"]["name"]

    def basic_dict(self, mongo_module, type):
        """Returns the part of the es schema that is the same for every object."""
        id = json.dumps(mongo_module["_id"])
        org = mongo_module["_id"]["org"]
        course = mongo_module["_id"]["course"]
        
        if not MONGO_COURSE_CACHE.get(course, False):
            MONGO_COURSE_CACHE[course] = self.course_name_from_mongo_module(mongo_module)
        offering = MONGO_COURSE_CACHE[course]

        course_id = "/".join([org, course, offering])
        hash = hashlib.sha1(id).hexdigest()
        display_name = (
            mongo_module.get("metadata", {"display_name": ""}).get("display_name", "") +
            " (" + mongo_module["_id"]["course"] + ")"
        )
        searchable_text = self.get_searchable_text(mongo_module, type)
        thumbnail = self.get_thumbnail(mongo_module, type)
        type_hash = hashlib.sha1(course_id).hexdigest()
        return {
            "id": id,
            "hash": hash,
            "display_name": display_name,
            "course_id": course_id,
            "searchable_text": searchable_text,
            "thumbnail": thumbnail,
            "type_hash": type_hash
        }

    def get_searchable_text(self, mongo_module, type):
        """Returns searchable text for a module. Defined for a module only"""
        if type.lower() == "pdf":
            name = re.sub(r'(.*?)(/asset/)(.*?)(\.pdf)(.*?)$', r'\3'+".pdf", mongo_module["definition"]["data"])
            asset = self.find_asset_with_name(name)
            if not asset:
                searchable_text = ""
            else:
                searchable_text = self.pdf_to_text(asset)
        elif type.lower() == "problem":
            searchable_text = self.searchable_text_from_problem_data(mongo_module)
        elif type.lower() == "transcript":
            searchable_text = self.find_transcript_for_video_module(mongo_module)
        return searchable_text

    def get_thumbnail(self, mongo_module, type):
        if type.lower() == "pdf":
            try:
                name = re.sub(r'(.*?)(/asset/)(.*?)(\.pdf)(.*?)$', r'\3'+".pdf", mongo_module["definition"]["data"])
                asset = self.find_asset_with_name(name)
                if asset is None:
                    raise DelegateError
                thumbnail = self.thumbnail_from_pdf(asset.get("data", "").__str__())
            except (DelegateError, MissingDelegateError, CorruptImageError):
                thumbnail = ""
        elif type.lower() == "problem":
            thumbnail = self.thumbnail_from_html(mongo_module["definition"]["data"])
        elif type.lower() == "transcript":
            thumbnail = self.thumbnail_from_video_module(mongo_module)
        return thumbnail

    def index_all_pdfs(self, index, bulk_chunk=100):
        cursor = self.find_categories_with_regex("html", re.compile(".*?/asset/.*?\.pdf.*?"))
        for i in range(cursor.count()):
            item = cursor.next()
            data = self.basic_dict(item, "pdf")
            print self.es_instance.index_data(index, data)._content

    def index_all_problems(self, index, bulk_chunk=100):
        cursor = self.find_modules_by_category("problem")
        bulk_string = ""
        for i in range(cursor.count()):
            print i
            item = cursor.next()
            try:
                data = self.basic_dict(item, "problem")
            except IOError:  # In case the connection is refused for whatever reason, try again
                data = self.basic_dict(item, "problem")
            bulk_string += json.dumps({"index": {"_index": index, "_type": data["type_hash"], "_id": data["hash"]}})
            bulk_string += "\n"
            bulk_string += json.dumps(data)
            bulk_string += "\n"
            if i % bulk_chunk == 0:
                print self.es_instance.bulk_index(bulk_string)
                bulk_string = ""
        print self.es_instance.bulk_index(bulk_string)

    def index_all_transcripts(self, index, bulk_chunk=100):
        cursor = self.find_modules_by_category("video")
        bulk_string = ""
        for i in range(cursor.count()):
            print i
            item = cursor.next()
            if i == 1274:
                print json.dumps(item["_id"])
            try:
                data = self.basic_dict(item, "transcript")
            except IOError:  # In case the connection is refused for whatever reason, try again
                data = self.basic_dict(item, "transcript")
            bulk_string += json.dumps({"index": {"_index": index, "_type": data["type_hash"], "_id": data["hash"]}})
            bulk_string += "\n"
            bulk_string += json.dumps(data)
            bulk_string += "\n"
            if i % bulk_chunk == 0:
                print self.es_instance.bulk_index(bulk_string)._content
                bulk_string = ""
        print self.es_instance.bulk_index(bulk_string)._content

    def index_course(self, course):
        cursor = self.find_modules_for_course(course)
        for i in range(cursor.count()):
            item = cursor.next()
            category = item["_id"]["category"].lower().strip()
            data = {}
            index = ""
            if category == "video":
                data = self.basic_dict(item, "transcript")
                index = "transcript-index"
            elif category == "problem":
                data = self.basic_dict(item, "problem")
                index = "problem-index"
            elif category == "html":
                pattern = re.compile(".*?/asset/.*?\.pdf.*?")
                if pattern.match(item["definition"]["data"]):
                    data = self.basic_dict(item, "pdf")
                else:
                    data = {"test": ""}
                index = "pdf-index"
            else:
                continue
            if filter(None, data.values()) == data.values():
                print self.es_instance.index_data(index, item["_id"]["course"], data, data["hash"])._content


class PyGrep:

    def __init__(self, directory):
        self.directory = directory

    def grab_all_files_with_ending(self, file_ending):
        """Will return absolute paths to all files with given file ending in self.directory"""
        walk_results = os.walk(self.directory)
        file_check = lambda walk: len(walk[2]) > 0
        ending_prelim = lambda walk: file_ending in " ".join(walk[2])
        relevant_results = (entry for entry in walk_results if file_check(entry) and ending_prelim(entry))
        return (self.grab_files_from_os_walk(result, file_ending) for result in relevant_results)

    def grab_files_from_os_walk(self, os_walk_tuple, file_ending):
        """Made to interface with """
        format_check = lambda file_name: file_ending in file_name
        directory, subfolders, file_paths = os_walk_tuple
        return [os.path.join(directory, file_path) for file_path in file_paths if format_check(file_path)]


class EnchantDictionary:

    def __init__(self, esDatabase):
        self.es_instance = esDatabase

    def produce_dictionary(self, output_file, **kwargs):
        """Produces a dictionary or updates it depending on kwargs
        If no kwargs are given then this method will write a full dictionary including all
        entries in all indices and types and output it in an enchant-friendly way to the output file.

        Accepted kwargs are index, and source_file. If you want to index multiple types
        or indices you should pass them in as comma delimited strings. Source file should be
        an absolute path to an existing enchant-friendly dictionary file.

        max_results will also set the maximum number of entries to be used in generating the dictionary.
        Set to 50k by default"""

        index = kwargs.get("index", "_all")
        max_results = kwargs.get("max_results", 50000)
        words = set()
        if kwargs.get("source_file", None):
            words = set(open(kwargs["source_file"]).readlines())
        url = "/".join([self.es_instance.url, index, "_search?size="+str(max_results)+"&q=*.*"])
        response = requests.get(url)
        misses = 0
        hits = 0
        for entry in json.loads(response._content)['hits']['hits']:
            if entry["_source"].get("searchable_text", False):
                text = entry["_source"]["searchable_text"]
                if isinstance(text, list):
                    text = " ".join(text)
                words |= set(re.findall(r'[a-z]+', text.lower()))
                hits += 1
                print "HITS:" + str(hits)
            else:
                misses += 1
                print misses
                continue
        with open(output_file, 'wb') as dictionary:
            for word in words:
                dictionary.write(word+"\n")


if sys.argv[1] == "regenerate":
    mongo = MongoIndexer(content_database="edge-xcontent", module_database="edge-xmodule")
    mongo2 = MongoIndexer()

    edb = ElasticDatabase()

    if "pdf" in sys.argv[2:]:
        print edb.delete_index("pdf-index")
        mongo.index_all_pdfs("pdf-index")
        mongo2.index_all_pdfs("pdf-index")

    if "transcript" in sys.argv[2:]:
        print edb.delete_index("transcript-index")
        mongo.index_all_transcripts("transcript-index")
        mongo2.index_all_transcripts("transcript-index")
    
    if "problem" in sys.argv[2:]:
        print edb.delete_index("problem-index")
        mongo.index_all_problems("problem-index")
        mongo2.index_all_problems("problem-index")
    
    #print test.setup_type("transcript", "cleaning", mapping)._content
    #print test.get_type_mapping("transcript-index", "2-1x")
    #print test.index_directory_transcripts("/home/slater/edx_all/data", "transcript-index", "transcript")
