"""Parse ISA-Tab structured metadata describing experimental data.
Works with ISA-Tab (http://isatab.sourceforge.net), which provides a structured
format for describing experimental metdata.
The entry point for the module is the parse function, which takes an ISA-Tab
directory (or investigator file) to parse. It returns a ISATabRecord object
which contains details about the investigation. This is top level information
like associated publications and contacts.
This record contains a list of associated studies (ISATabStudyRecord objects).
Each study contains a metadata attribute, which has the key/value pairs
associated with the study in the investigation file. It also contains other
high level data like publications, contacts, and details about the experimental
design.
The nodes attribute of each record captures the information from the Study file.
This is a dictionary, where the keys are sample names and the values are
NodeRecord objects. This collapses the study information on samples, and
contains the associated information of each sample as key/value pairs in the
metadata attribute.
Finally, each study contains a list of assays, as ISATabAssayRecord objects.
Similar to the study objects, these have a metadata attribute with key/value
information about the assay. They also have a dictionary of nodes with data from
the Assay file; in assays the keys are raw data files.
This is a biased representation of the Study and Assay files which focuses on
collapsing the data across the samples and raw data.
"""
from __future__ import with_statement

import os
import re
import csv
import glob
import collections
import pprint
import bisect


def find_lt(a, x):
    """Find rightmost value less than x"""
    i = bisect.bisect_left(a, x)
    if i:
        return a[i-1]
    raise ValueError


def find_gt(a, x):
    "Find leftmost item greater than or equal to x"
    i = bisect.bisect_left(a, x)
    if i != len(a):
        return a[i]
    raise ValueError


def parse(isatab_ref):
    """Entry point to parse an ISA-Tab directory.
    isatab_ref can point to a directory of ISA-Tab data, in which case we
    search for the investigator file, or be a reference to the high level
    investigation file.
    """
    if os.path.isdir(isatab_ref):
        fnames = glob.glob(os.path.join(isatab_ref, "i_*.txt")) + \
                 glob.glob(os.path.join(isatab_ref, "*.idf.txt"))
        assert len(fnames) == 1
        isatab_ref = fnames[0]
    assert os.path.exists(isatab_ref), "Did not find investigation file: %s" % isatab_ref
    i_parser = InvestigationParser()
    with open(isatab_ref, "rU") as in_handle:
        rec = i_parser.parse(in_handle)
    s_parser = StudyAssayParser(isatab_ref)
    rec = s_parser.parse(rec)
    return rec


class InvestigationParser:
    """Parse top level investigation files into ISATabRecord objects.
    """
    def __init__(self):
        self._sections = {
            "ONTOLOGY SOURCE REFERENCE": "ontology_refs",
            "INVESTIGATION": "metadata",
            "INVESTIGATION PUBLICATIONS": "publications",
            "INVESTIGATION CONTACTS": "contacts",
            "STUDY DESIGN DESCRIPTORS": "design_descriptors",
            "STUDY PUBLICATIONS": "publications",
            "STUDY FACTORS": "factors",
            "STUDY ASSAYS" : "assays",
            "STUDY PROTOCOLS" : "protocols",
            "STUDY CONTACTS": "contacts"}
        self._nolist = ["metadata"]

    def parse(self, in_handle):
        line_iter = self._line_iter(in_handle)
        # parse top level investigation details
        rec = ISATabRecord()
        rec, _ = self._parse_region(rec, line_iter)

        # parse study information
        while 1:
            study = ISATabStudyRecord()
            study, had_info = self._parse_region(study, line_iter)
            if had_info:
                rec.studies.append(study)
            else:
                break
        # handle SDRF files for MAGE compliant ISATab
        if rec.metadata.has_key("SDRF File"):
            study = ISATabStudyRecord()
            study.metadata["Study File Name"] = rec.metadata["SDRF File"]
            rec.studies.append(study)
        return rec

    def _parse_region(self, rec, line_iter):
        """Parse a section of an ISA-Tab, assigning information to a supplied record.
        """
        had_info = False
        keyvals, section = self._parse_keyvals(line_iter)

        if keyvals:
            rec.metadata = keyvals[0]
        while section and section[0] != "STUDY":
            had_info = True
            keyvals, next_section = self._parse_keyvals(line_iter)
            attr_name = self._sections[section[0]]
            if attr_name in self._nolist:
                try:
                    keyvals = keyvals[0]
                except IndexError:
                    keyvals = {}

            setattr(rec, attr_name, keyvals)
            section = next_section
        return rec, had_info

    def _line_iter(self, in_handle):
        """Read tab delimited file, handling ISA-Tab special case headers.
        """
        reader = csv.reader(in_handle, dialect="excel-tab")
        for line in reader:
            if len(line) > 0 and line[0]:
                # check for section headers; all uppercase and a single value
                if line[0].upper() == line[0] and "".join(line[1:]) == "":
                    line = [line[0]]
                yield line

    def _parse_keyvals(self, line_iter):
        """Generate dictionary from key/value pairs.
        """
        out = None
        line = None
        for line in line_iter:
            if len(line) == 1 and line[0].upper() == line[0]:
                break
            else:
                # setup output dictionaries, trimming off blank columns
                if out is None:
                    while not line[-1]:
                        line = line[:-1]
                    out = [{} for _ in line[1:]]
                # add blank values if the line is stripped
                while len(line) < len(out) + 1:
                    line.append("")
                for i in range(len(out)):
                    out[i][line[0]] = line[i+1].strip()
                line = None
        return out, line


class StudyAssayParser:
    """Parse row oriented metadata associated with study and assay samples.
    This currently does not attempt to be complete, but rather to extract the
    most useful information (in my biased opinion) and represent it simply
    in the record objects.
    This is coded generally, so can be expanded to more cases. It is biased
    towards microarray and next-gen sequencing data.
    """
    def __init__(self, base_file):
        self._dir = os.path.dirname(base_file)
        self._col_quals = ("Performer", "Date", "Unit",
                           "Term Accession Number", "Term Source REF")
        self._col_types = {"attribute": ("Characteristics", "Factor Type",
                                         "Comment", "Label", "Material Type", "Factor Value"),
                           "node" : ("Sample Name", "Source Name", "Image File",
                                     "Raw Data File", "Derived Data File"),
                           "node_assay" : ("Extract Name", "Labeled Extract Name",
                                           "Assay Name", "Data Transformation Name",
                                           "Normalization Name"),
                           "processing": ("Protocol REF",)}
        self._synonyms = {"Array Data File" : "Raw Data File",
                          "Derived Array Data File" : "Derived Data File",
                          "Hybridization Assay Name": "Assay Name",
                          "Derived Array Data Matrix File": "Derived Data File",
                          "Raw Spectral Data File": "Raw Data File",
                          "Derived Spectral Data File": "Derived Data File"}

    def parse(self, rec):
        """Retrieve row data from files associated with the ISATabRecord.
        """
        final_studies = []
        for study in rec.studies:
            source_data = self._parse_study(study.metadata["Study File Name"],
                                            ["Source Name","Sample Name", "Comment[ENA_SAMPLE]"])
            if source_data:
                study.nodes = source_data
                final_assays = []
                for assay in study.assays:
                    cur_assay = ISATabAssayRecord(assay)
                    assay_data = self._parse_study(assay["Study Assay File Name"],
                                                   ["Raw Data File", "Derived Data File",
                                                    "Image File"])
                    cur_assay.nodes = assay_data
                    final_assays.append(cur_assay)
                study.assays = final_assays

                #get process nodes
                self._get_process_nodes(study.metadata["Study File Name"], study)
                final_studies.append(study)
        rec.studies = final_studies
        return rec

    def _get_process_nodes(self, fname, study):
        print "in _get_process_nodes...", fname
        if not os.path.exists(os.path.join(self._dir, fname)):
            return None
        process_nodes = {}

        with open(os.path.join(self._dir, fname), "rU") as in_handle:
            reader = csv.reader(in_handle, dialect="excel-tab")
            headers = self._swap_synonyms(reader.next())
            hgroups = self._collapse_header(headers)
            htypes = self._characterize_header(headers, hgroups)

            print "headers ->", headers
            print "hgroups ->", hgroups
            processing_indices = [i for i, x in enumerate(htypes) if x == "processing"]
            node_indices = [i for i, x in enumerate(htypes) if x == "node"]

            print "processing_indices==",processing_indices
            print "node_indices==", node_indices

            for processing_index in processing_indices:
                print "processing_index", processing_index
                input_index = find_lt(node_indices, processing_index)
                output_index = find_gt(node_indices, processing_index)
                print "input_index==", input_index
                print "output_index==", output_index

                input_header = headers[hgroups[input_index][0]]
                output_header = headers[hgroups[output_index][0]]
                # print "input header ", input_header
                # print "output header ", output_header

                processing_header = headers[hgroups[processing_index][0]]

                for line in reader:
                    input_name = line[hgroups[input_index][0]]
                    output_name = line[hgroups[output_index][0]]
                    processing_name = line[hgroups[processing_index][0]]
                    print "input_name--->",  input_name
                    print "output_name--->",  output_name
                    print "processing_name--->",  processing_name

                    print study

                    input_node = study.nodes[input_name]
                    #print "input_node==>", input_node

                    process_node = ProcessNodeRecord(processing_name, processing_header)
                    process_node.outputs = input_node.metadata[output_header]
                    process_node.inputs = input_node.metadata[input_header]

                    print "ProcessNode ---> ", process_node

                    break


                # input = study.nodes.metadata[input_header]
                # output = study.nodes.metadata[output_header]
                #
                # print "input ->", input
                # print "output ->", output

    def _parse_study(self, fname, node_types):
        """Parse study or assay row oriented file around the supplied base node.
        """
        if not os.path.exists(os.path.join(self._dir, fname)):
            return None
        nodes = {}
        with open(os.path.join(self._dir, fname), "rU") as in_handle:
            reader = csv.reader(in_handle, dialect="excel-tab")
            header = self._swap_synonyms(reader.next())
            hgroups = self._collapse_header(header)
            htypes = self._characterize_header(header, hgroups)
            for node_type in node_types:
                try:
                    name_index = header.index(node_type)
                    break
                except ValueError:
                    name_index = None
            assert name_index is not None, "Could not find standard header name: %s in %s" \
                   % (node_types, header)
            for line in reader:
                name = line[name_index]
                try:
                    node = nodes[name]
                except KeyError:
                    node = NodeRecord(name, node_type)
                    node.metadata = collections.defaultdict(set)
                    nodes[name] = node
                attrs = self._line_keyvals(line, header, hgroups, htypes,
                                           node.metadata)
                nodes[name].metadata = attrs
        return dict([(k, self._finalize_metadata(v)) for k, v in nodes.items()])

    # def _parse_study(self, fname, node_types, data_nodes):
    #     """Parse study or assay row oriented file around the supplied base node.
    #     """
    #     if not os.path.exists(os.path.join(self._dir, fname)):
    #         return None
    #
    #     nodes = {}
    #     with open(os.path.join(self._dir, fname), "rU") as in_handle:
    #         reader = csv.reader(in_handle, dialect="excel-tab")
    #         header = self._swap_synonyms(reader.next())
    #         hgroups = self._collapse_header(header)
    #         htypes = self._characterize_header(header, hgroups)
    #         for node_type in node_types:
    #             try:
    #                 name_index = header.index(node_type)
    #                 break
    #             except ValueError:
    #                 name_index = None
    #         assert name_index is not None, "Could not find standard header name: %s in %s" \
    #                % (node_types, header)
    #         for line in reader:
    #             name = line[name_index]
    #             try:
    #                 node = nodes[name]
    #             except KeyError:
    #                 if node_type == "Protocol REF":
    #                     node = ProcessNodeRecord(name, node_type)
    #                     nodes[name] = node
    #                 else:
    #                     node = NodeRecord(name, node_type)
    #                     node.metadata = collections.defaultdict(set)
    #                     nodes[name] = node
    #
    #             if node_type == "Protocol REF":
    #                  inputs = {}
    #                  outputs = {}
    #                  #self._get_inputs_outputs(line, header, hgroups, htypes, inputs, outputs, data_nodes)
    #                  #print "inputs--->", inputs
    #                  #print "outputs--->", outputs
    #             else:
    #                 attrs = self._line_keyvals(line, header, hgroups, htypes,
    #                                        node.metadata)
    #                 nodes[name].metadata = attrs
    #
    #     print "nodes before returning from _parse_study --->", nodes.items()
    #
    #     return dict([(k, self._finalize_metadata(v)) for k, v in nodes.items()])

    def _finalize_metadata(self, node):
        """Convert node metadata back into a standard dictionary and list.
        """
        final = {}
        for key, val in node.metadata.iteritems():
            #val = list(val)
            #if isinstance(val[0], tuple):
            #    val = [dict(v) for v in val]
            final[key] = list(val)
        node.metadata = final
        return node

    def _get_inputs_outputs(self, line, header, hgroups, htypes, inputs, outputs, data_nodes):
        print "_get_inputs--->"
        processing_indices = [i for i, x in enumerate(htypes) if x == "processing"]
        node_indices = [i for i, x in enumerate(htypes) if x == "node"]

        print "data_nodes--->", data_nodes
        print "processing_indices--->",processing_indices
        print "node_indices--->",node_indices
        return

        # if len(line) == 0:
        #   return (inputs, outputs)
        # else:
        #     try:
        #         processing_index = htypes.index('processing')
        #     except ValueError:
        #         print "No processing node"
        #         return
        #
        #     data_node_index = htypes.index('node')
        #
        #     print "data_node_index-->", data_node_index
        #     print "data node name--->", line[data_node_index]
        #     print "data node--->", data_nodes[line[data_node_index]]
        #
        #     print "processing_index--->", processing_index
        #
        #     if data_node_index < processing_index:
        #         #add inputs
        #         #print "data nodes--->", data_nodes
        #
        #         inputs = data_nodes[line[data_node_index]]
        #         hgroups_index = hgroups[processing_index][0] +1
        #         line = line[hgroups_index:]
        #     else:
        #         outputs = data_nodes[line[data_node_index]]
        #         hgroups_index = hgroups[data_node_index][0] +1
        #         line = line[hgroups_index:]
        #
        #     print "new line--->", line
        #     return self._get_inputs_outputs(line,header, hgroups, htypes, inputs, outputs, data_nodes)


    def _line_keyvals(self, line, header, hgroups, htypes, out):
        out = self._line_by_type(line, header, hgroups, htypes, out, "attribute",
                                 self._collapse_attributes)
        out = self._line_by_type(line, header, hgroups, htypes, out, "processing",
                                 self._collapse_attributes)
        out = self._line_by_type(line, header, hgroups, htypes, out, "node")
        return out

    def _line_by_type(self, line, header, hgroups, htypes, out, want_type,
                      collapse_quals_fn = None):
        """Parse out key value pairs for line information based on a group of values.
        """
        for index, htype in ((i, t) for i, t in enumerate(htypes) if t == want_type):
            col = hgroups[index][0]
            key = self._clean_header(header[col])
            if collapse_quals_fn:
                val = collapse_quals_fn(line, header, hgroups[index])
            else:
                val = line[col]
            out[key].add(val)
        return out

    def _collapse_attributes(self, line, header, indexes):
        """Combine attributes in multiple columns into single named tuple.
        """
        names = []
        vals = []
        pat = re.compile("[\W]+")
        for i in indexes:
            names.append(pat.sub("_", self._clean_header(header[i])))
            vals.append(line[i])
        Attrs = collections.namedtuple('Attrs', names)
        return Attrs(*vals)

    def _clean_header(self, header):
        """Remove ISA-Tab specific information from Header[real name] headers.
        """
        if header.find("[") >= 0:
            header = header.replace("]", "").split("[")[-1]
        # ISATab can start with numbers but this is not supported in
        # the python datastructure, so prefix with isa_ to make legal
        try:
            int(header[0])
            header = "isa_" + header
        except ValueError:
            pass
        return header

    def _characterize_header(self, header, hgroups):
        """Characterize header groups into different data types.
        """
        out = []
        for h in [header[g[0]] for g in hgroups]:
            this_ctype = None
            for ctype, names in self._col_types.iteritems():
                if h.startswith(names):
                    this_ctype = ctype
                    break
            out.append(this_ctype)
        return out

    def _collapse_header(self, header):
        """Combine header columns into related groups.
        """
        out = []
        for i, h in enumerate(header):
            if h.startswith(self._col_quals):
                out[-1].append(i)
            else:
                out.append([i])
        return out

    def _swap_synonyms(self, header):
        return [self._synonyms.get(h, h) for h in header]

_record_str = \
"""* ISATab Record
 metadata: {md}
 studies:
{studies}
"""

_study_str = \
"""  * Study
   metadata: {md}
   design_descriptors: {design_descriptors}
   publications : {publications}
   factors: {factors}
   protocols: {protocols}
   nodes:
    {nodes}
   process_nodes:
    {process_nodes}
   assays:
{assays}
"""

_assay_str = \
"""    * Assay
     metadata: {md}
     nodes:
        {nodes}
     process_nodes:
       {process_nodes}
"""

_node_str = \
"""       * Node -> {name} {type}
         metadata: {md}"""

_process_node_str = \
"""       * Process Node ->  {name} {type}
         inputs: {inputs}
         outputs: {outputs}
         """


class ISATabRecord:
    """Represent ISA-Tab metadata in structured format.
    High level key/value data.
      - metadata -- dictionary
      - ontology_refs -- list of dictionaries
      - contacts -- list of dictionaries
      - publications -- list of dictionaries
    Sub-elements:
      - studies: List of ISATabStudyRecord objects.
    """
    def __init__(self):
        self.metadata = {}
        self.ontology_refs = []
        self.publications = []
        self.contacts = []
        self.studies = []

    def __str__(self):
        return _record_str.format(md=pprint.pformat(self.metadata).replace("\n", "\n" + " " * 3),
                                  ont=self.ontology_refs,
                                  pub=self.publications,
                                  contact=self.contacts,
                                  studies="\n".join(str(x) for x in self.studies))

class ISATabStudyRecord:
    """Represent a study within an ISA-Tab record.
    """
    def __init__(self):
        self.metadata = {}
        self.design_descriptors = []
        self.publications = []
        self.factors = []
        self.assays = []
        self.protocols = []
        self.contacts = []
        self.nodes = {}
        self.process_nodes = {}

    def __str__(self):
        return _study_str.format(md=pprint.pformat(self.metadata).replace("\n", "\n" + " " * 5),
                                 design_descriptors=pprint.pformat(self.design_descriptors).replace("\n", "\n" + " " * 5),
                                 publications="\n".join(str(x) for x in self.publications),
                                 factors="\n".join(str(x) for x in self.factors),
                                 assays="\n".join(str(x) for x in self.assays),
                                 protocols="\n".join(str(x) for x in self.protocols),
                                 nodes="\n".join(str(x) for x in self.nodes.values()),
                                 process_nodes="\n".join(str(x) for x in self.process_nodes.values())
        )

class ISATabAssayRecord:
    """Represent an assay within an ISA-Tab record.
    """
    def __init__(self, metadata=None):
        if metadata is None: metadata = {}
        self.metadata = metadata
        self.nodes = {}
        self.process_nodes = {}

    def __str__(self):
        return _assay_str.format(md=pprint.pformat(self.metadata).replace("\n", "\n" + " " * 7),
                                 nodes="\n".join(str(x) for x in self.nodes.values()),
                                 process_nodes="\n".join(str(x) for x in self.process_nodes.values())
        )

class NodeRecord:
    """Represent a data or material node within an ISA-Tab Study/Assay file.
    """
    def __init__(self, name="", ntype=""):
        self.ntype = ntype
        self.name = name
        self.metadata = {}

    def __str__(self):
        return _node_str.format(md=pprint.pformat(self.metadata).replace("\n", "\n" + " " * 9),
                                name=self.name,
                                type=self.ntype)


class ProcessNodeRecord:
    """Represent a process node within an ISA-Tab Study/Assay file.
    """
    def __init__(self, name="", ntype=""):
        self.ntype = ntype
        self.name = name
        self.inputs = {}
        self.outputs = {}

    def __str__(self):
        return _process_node_str.format(inputs=pprint.pformat(self.inputs).replace("\n", "\n" + " " * 9),
                                outputs=pprint.pformat(self.outputs).replace("\n", "\n" + " " * 9),
                                name=self.name,
                                type=self.ntype)
