# -*- coding: utf-8 -*-
from __future__ import division

__copyright__ = """
Copyright (C) 2013 Andreas Kloeckner
Copyright (C) 2016 Matt Wala"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
import pyopencl as cl
import pyopencl.array  # noqa
from mako.template import Template
from boxtree.tools import AXIS_NAMES, DeviceDataRecord
from pytools import memoize_method

import logging
logger = logging.getLogger(__name__)


__doc__ = """
Area queries (Balls -> overlapping leaves)
------------------------------------------

.. autoclass:: AreaQueryBuilder

.. autoclass:: AreaQueryResult


Inverse of area query (Leaves -> overlapping balls)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autoclass:: LeavesToBallsLookupBuilder

.. autoclass:: LeavesToBallsLookup


Space invader queries
^^^^^^^^^^^^^^^^^^^^^

.. autoclass:: SpaceInvaderQueryBuilder


Peer Lists
^^^^^^^^^^

Area queries are implemented using peer lists.

.. autoclass:: PeerListFinder

.. autoclass:: PeerListLookup

"""


# {{{ output

class PeerListLookup(DeviceDataRecord):
    """
    .. attribute:: tree

        The :class:`boxtree.Tree` instance used to build this lookup.

    .. attribute:: peer_list_starts

        Indices into :attr:`peer_lists`.
        ``peer_lists[peer_list_starts[box_id]:peer_list_starts[box_id]+1]``
        contains the list of peer boxes of box `box_id`.

    .. attribute:: peer_lists

    .. automethod:: get

    .. versionadded:: 2016.1
    """


class AreaQueryResult(DeviceDataRecord):
    """
    .. attribute:: tree

        The :class:`boxtree.Tree` instance used to build this lookup.

    .. attribute:: leaves_near_ball_starts

        Indices into :attr:`leaves_near_ball_lists`.
        ``leaves_near_ball_lists[leaves_near_ball_starts[ball_nr]:
        leaves_near_ball_starts[ball_nr]+1]``
        results in a list of leaf boxes that intersect `ball_nr`.

    .. attribute:: leaves_near_ball_lists

    .. automethod:: get

    .. versionadded:: 2016.1
    """


class LeavesToBallsLookup(DeviceDataRecord):
    """
    .. attribute:: tree

        The :class:`boxtree.Tree` instance used to build this lookup.

    .. attribute:: balls_near_box_starts

        Indices into :attr:`balls_near_box_lists`.
        ``balls_near_box_lists[balls_near_box_starts[ibox]:
        balls_near_box_starts[ibox]+1]``
        results in a list of balls that overlap leaf box *ibox*.

        .. note:: Only leaf boxes have non-empty entries in this table. Nonetheless,
            this list is indexed by the global box index.

    .. attribute:: balls_near_box_lists

    .. automethod:: get
    """

# }}}


# {{{ kernel templates

GUIDING_BOX_FINDER_MACRO = r"""//CL:mako//
    <%def name="find_guiding_box(ball_center, ball_radius, box='guiding_box')">
        box_id_t ${box} = 0;

        // Descend when root is not the guiding box.
        if (LEVEL_TO_RAD(0) / 2 >= ${ball_radius})
        {
            for (unsigned box_level = 0;; ++box_level)
            {
                if (/* Found leaf? */
                    !(box_flags[${box}] & BOX_HAS_CHILDREN)
                    /* Found guiding box? */
                    || (LEVEL_TO_RAD(box_level) / 2 < ${ball_radius}
                        && ${ball_radius} <= LEVEL_TO_RAD(box_level)))
                {
                    break;
                }

                // Find the child containing the ball center.
                //
                // Logic intended to match the morton nr scan kernel.

                %for ax in AXIS_NAMES[:dimensions]:
                    unsigned ${ax}_bits = (unsigned) (
                        ((${ball_center}.${ax} - bbox_min_${ax}) / root_extent)
                        * (1U << (1 + box_level)));
                %endfor

                // Pick off the lowest-order bit for each axis, put it in its place.
                int level_morton_number = 0
                %for iax, ax in enumerate(AXIS_NAMES[:dimensions]):
                    | (${ax}_bits & 1U) << (${dimensions-1-iax})
                %endfor
                    ;

                ${box} = box_child_ids[
                    level_morton_number * aligned_nboxes + ${box}];
            }
        }
    </%def>
"""


AREA_QUERY_WALKER_BODY = r"""
    coord_vec_t ball_center;
    coord_t ball_radius;
    ${get_ball_center_and_radius("ball_center", "ball_radius", "i")}

    ///////////////////////////////////
    // Step 1: Find the guiding box. //
    ///////////////////////////////////

    ${find_guiding_box("ball_center", "ball_radius")}

    //////////////////////////////////////////////////////
    // Step 2 - Walk the peer boxes to find the leaves. //
    //////////////////////////////////////////////////////

    for (peer_list_idx_t pb_i = peer_list_starts[guiding_box],
         pb_e = peer_list_starts[guiding_box+1]; pb_i < pb_e; ++pb_i)
    {
        box_id_t peer_box = peer_lists[pb_i];

        if (!(box_flags[peer_box] & BOX_HAS_CHILDREN))
        {
            ${leaf_found_op("peer_box", "ball_center", "ball_radius")}
        }
        else
        {
            ${walk_init("peer_box")}

            while (continue_walk)
            {
                box_id_t child_box_id = box_child_ids[
                    walk_morton_nr * aligned_nboxes + walk_box_id];

                if (child_box_id)
                {
                    if (!(box_flags[child_box_id] & BOX_HAS_CHILDREN))
                    {
                        ${leaf_found_op("child_box_id", "ball_center",
                                        "ball_radius")}
                    }
                    else
                    {
                        // We want to descend into this box. Put the current state
                        // on the stack.
                        ${walk_push("child_box_id")}
                        continue;
                    }
                }

                ${walk_advance()}
            }
        }
    }
"""


AREA_QUERY_TEMPLATE = (
    GUIDING_BOX_FINDER_MACRO + r"""//CL//
    typedef ${dtype_to_ctype(ball_id_dtype)} ball_id_t;
    typedef ${dtype_to_ctype(peer_list_idx_dtype)} peer_list_idx_t;

    <%def name="get_ball_center_and_radius(ball_center, ball_radius, i)">
        %for ax in AXIS_NAMES[:dimensions]:
            ${ball_center}.${ax} = ball_${ax}[${i}];
        %endfor
       ${ball_radius} = ball_radii[${i}];
    </%def>

    <%def name="leaf_found_op(leaf_box_id, ball_center, ball_radius)">
        {
            bool is_overlapping;

            ${check_l_infty_ball_overlap(
                "is_overlapping", leaf_box_id, ball_radius, ball_center)}

            if (is_overlapping)
            {
                APPEND_leaves(${leaf_box_id});
            }
        }
    </%def>

    void generate(LIST_ARG_DECL USER_ARG_DECL ball_id_t i)
    {
    """ +
    AREA_QUERY_WALKER_BODY +
    """
    }
    """)


PEER_LIST_FINDER_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t box_id)
{
    ${load_center("center", "box_id")}

    if (box_id == 0)
    {
        // Peer of root = self
        APPEND_peers(box_id);
        return;
    }

    int level = box_levels[box_id];

    // To find this box's peers, start at the top of the tree, descend
    // into adjacent (or overlapping) parents.
    ${walk_init(0)}

    while (continue_walk)
    {
        box_id_t child_box_id = box_child_ids[
                walk_morton_nr * aligned_nboxes + walk_box_id];

        if (child_box_id)
        {
            ${load_center("child_center", "child_box_id")}

            // child_box_id lives on walk_level+1.
            bool a_or_o = is_adjacent_or_overlapping(root_extent,
                center, level, child_center, walk_level+1, false);

            if (a_or_o)
            {
                // child_box_id lives on walk_level+1.
                if (walk_level+1 == level)
                {
                    APPEND_peers(child_box_id);
                }
                else if (!(box_flags[child_box_id] & BOX_HAS_CHILDREN))
                {
                    APPEND_peers(child_box_id);
                }
                else
                {
                    // Check if any children are adjacent or overlapping.
                    // If not, this box must be a peer.
                    bool must_be_peer = true;

                    for (int morton_nr = 0;
                         must_be_peer && morton_nr < ${2**dimensions};
                         ++morton_nr)
                    {
                        box_id_t next_child_id = box_child_ids[
                            morton_nr * aligned_nboxes + child_box_id];
                        if (next_child_id)
                        {
                            ${load_center("next_child_center", "next_child_id")}
                            must_be_peer &= !is_adjacent_or_overlapping(root_extent,
                                center, level, next_child_center, walk_level+2,
                                false);
                        }
                    }

                    if (must_be_peer)
                    {
                        APPEND_peers(child_box_id);
                    }
                    else
                    {
                        // We want to descend into this box. Put the current state
                        // on the stack.
                        ${walk_push("child_box_id")}
                        continue;
                    }
                }
            }
        }

        ${walk_advance()}
    }
}

"""


from pyopencl.elementwise import ElementwiseTemplate
from boxtree.tools import InlineBinarySearch


STARTS_EXPANDER_TEMPLATE = ElementwiseTemplate(
    arguments=r"""
        idx_t *dst,
        idx_t *starts,
        idx_t starts_len
    """,
    operation=r"""//CL//
    /* Find my index in starts, place the index in dst. */
    dst[i] = bsearch(starts, starts_len, i);
    """,
    name="starts_expander",
    preamble=str(InlineBinarySearch("idx_t")))

# }}}


# {{{ area query elementwise template

class AreaQueryElementwiseTemplate(object):
    """
    Experimental: Intended as a way to perform operations in the body of an area
    query.
    """

    @staticmethod
    def unwrap_args(tree, peer_lists, *args):
        return (tree.box_centers,
                tree.root_extent,
                tree.box_levels,
                tree.aligned_nboxes,
                tree.box_child_ids,
                tree.box_flags,
                peer_lists.peer_list_starts,
                peer_lists.peer_lists) + tuple(tree.bounding_box[0]) + args

    def __init__(self, extra_args, ball_center_and_radius_expr,
                 leaf_found_op, preamble="", name="area_query_elwise"):

        def wrap_in_macro(decl, expr):
            return """
            <%def name=\"{decl}\">
            {expr}
            </%def>
            """.format(decl=decl, expr=expr)

        from boxtree.traversal import TRAVERSAL_PREAMBLE_MAKO_DEFS

        self.elwise_template = ElementwiseTemplate(
            arguments=r"""//CL:mako//
                coord_t *box_centers,
                coord_t root_extent,
                box_level_t *box_levels,
                box_id_t aligned_nboxes,
                box_id_t *box_child_ids,
                box_flags_t *box_flags,
                peer_list_idx_t *peer_list_starts,
                box_id_t *peer_lists,
                %for ax in AXIS_NAMES[:dimensions]:
                    coord_t bbox_min_${ax},
                %endfor
            """ + extra_args,
            operation="//CL:mako//\n" +
            wrap_in_macro("get_ball_center_and_radius(ball_center, ball_radius, i)",
                          ball_center_and_radius_expr) +
            wrap_in_macro("leaf_found_op(leaf_box_id, ball_center, ball_radius)",
                          leaf_found_op) +
            TRAVERSAL_PREAMBLE_MAKO_DEFS +
            GUIDING_BOX_FINDER_MACRO +
            AREA_QUERY_WALKER_BODY,
            name=name,
            preamble=preamble)

    def generate(self, context,
                 dimensions, coord_dtype, box_id_dtype,
                 peer_list_idx_dtype, max_levels,
                 extra_var_values=(), extra_type_aliases=(),
                 extra_preamble=""):
        from pyopencl.tools import dtype_to_ctype
        from boxtree import box_flags_enum
        from boxtree.traversal import TRAVERSAL_PREAMBLE_TYPEDEFS_AND_DEFINES

        render_vars = (
            ("dimensions", dimensions),
            ("dtype_to_ctype", dtype_to_ctype),
            ("box_id_dtype", box_id_dtype),
            ("particle_id_dtype", None),
            ("coord_dtype", coord_dtype),
            ("vec_types", tuple(cl.array.vec.types.items())),
            ("max_levels", max_levels),
            ("AXIS_NAMES", AXIS_NAMES),
            ("box_flags_enum", box_flags_enum),
            ("peer_list_idx_dtype", peer_list_idx_dtype),
            ("debug", False),
            # Not used (but required by TRAVERSAL_PREAMBLE_TEMPLATE)
            ("stick_out_factor", 0),
        )

        preamble = Template(
            # HACK: box_flags_t and coord_t are defined here and
            # in the template below, so disable typedef redifinition warnings.
            """
            #pragma clang diagnostic push
            #pragma clang diagnostic ignored "-Wtypedef-redefinition"
            """ +
            TRAVERSAL_PREAMBLE_TYPEDEFS_AND_DEFINES +
            """
            #pragma clang diagnostic pop
            """,
            strict_undefined=True).render(**dict(render_vars))

        return self.elwise_template.build(context,
                type_aliases=(
                    ("coord_t", coord_dtype),
                    ("box_id_t", box_id_dtype),
                    ("peer_list_idx_t", peer_list_idx_dtype),
                    ("box_level_t", np.uint8),
                    ("box_flags_t", box_flags_enum.dtype),
                ) + extra_type_aliases,
                var_values=render_vars + extra_var_values,
                more_preamble=preamble + extra_preamble)


SPACE_INVADER_QUERY_TEMPLATE = AreaQueryElementwiseTemplate(
    extra_args="""
    coord_t *ball_radii,
    float *outer_space_invader_dists,
    %for ax in AXIS_NAMES[:dimensions]:
        coord_t *ball_${ax},
    %endfor
    """,
    ball_center_and_radius_expr=r"""
    ${ball_radius} = ball_radii[${i}];
    %for ax in AXIS_NAMES[:dimensions]:
        ${ball_center}.${ax} = ball_${ax}[${i}];
    %endfor
    """,
    leaf_found_op=r"""
    {
        ${load_center("leaf_center", leaf_box_id)}
        int leaf_level = box_levels[${leaf_box_id}];

        coord_t size_sum = LEVEL_TO_RAD(leaf_level) + ${ball_radius};

        coord_t max_dist = 0;
        %for i in range(dimensions):
            max_dist = fmax(max_dist,
                distance(${ball_center}.s${i}, leaf_center.s${i}));
        %endfor

        bool is_overlapping = max_dist <= size_sum;

        if (is_overlapping)
        {
            // The atomic max operation supports only integer types.
            // However, max_dist is of a floating point type.
            // For comparison purposes we reinterpret the bits of max_dist
            // as an integer. The comparison result is the same as for positive
            // IEEE floating point numbers, so long as the float/int endianness
            // matches (fingers crossed).
            atomic_max(
                (volatile __global int *)
                    &outer_space_invader_dists[${leaf_box_id}],
                as_int((float) max_dist));
        }
    }""",
    name="space_invader_query")

# }}}


# {{{ area query build

class AreaQueryBuilder(object):
    """Given a set of :math:`l^\infty` "balls", this class helps build a
    look-up table from ball to leaf boxes that intersect with the ball.

    .. versionadded:: 2016.1

    .. automethod:: __call__
    """
    def __init__(self, context):
        self.context = context
        self.peer_list_finder = PeerListFinder(self.context)

    # {{{ Kernel generation

    @memoize_method
    def get_area_query_kernel(self, dimensions, coord_dtype, box_id_dtype,
                              ball_id_dtype, peer_list_idx_dtype, max_levels):
        from pyopencl.tools import dtype_to_ctype
        from boxtree import box_flags_enum

        logger.info("start building area query kernel")

        from boxtree.traversal import TRAVERSAL_PREAMBLE_TEMPLATE

        template = Template(
            TRAVERSAL_PREAMBLE_TEMPLATE
            + AREA_QUERY_TEMPLATE,
            strict_undefined=True)

        render_vars = dict(
            dimensions=dimensions,
            dtype_to_ctype=dtype_to_ctype,
            box_id_dtype=box_id_dtype,
            particle_id_dtype=None,
            coord_dtype=coord_dtype,
            vec_types=cl.array.vec.types,
            max_levels=max_levels,
            AXIS_NAMES=AXIS_NAMES,
            box_flags_enum=box_flags_enum,
            peer_list_idx_dtype=peer_list_idx_dtype,
            ball_id_dtype=ball_id_dtype,
            debug=False,
            # Not used (but required by TRAVERSAL_PREAMBLE_TEMPLATE)
            stick_out_factor=0)

        from pyopencl.tools import VectorArg, ScalarArg
        arg_decls = [
            VectorArg(coord_dtype, "box_centers"),
            ScalarArg(coord_dtype, "root_extent"),
            VectorArg(np.uint8, "box_levels"),
            ScalarArg(box_id_dtype, "aligned_nboxes"),
            VectorArg(box_id_dtype, "box_child_ids"),
            VectorArg(box_flags_enum.dtype, "box_flags"),
            VectorArg(peer_list_idx_dtype, "peer_list_starts"),
            VectorArg(box_id_dtype, "peer_lists"),
            VectorArg(coord_dtype, "ball_radii"),
            ] + [
            ScalarArg(coord_dtype, "bbox_min_"+ax)
            for ax in AXIS_NAMES[:dimensions]
            ] + [
            VectorArg(coord_dtype, "ball_"+ax)
            for ax in AXIS_NAMES[:dimensions]]

        from pyopencl.algorithm import ListOfListsBuilder
        area_query_kernel = ListOfListsBuilder(
            self.context,
            [("leaves", box_id_dtype)],
            str(template.render(**render_vars)),
            arg_decls=arg_decls,
            name_prefix="area_query",
            count_sharing={},
            complex_kernel=True)

        logger.info("done building area query kernel")
        return area_query_kernel

    # }}}

    def __call__(self, queue, tree, ball_centers, ball_radii, peer_lists=None,
                 wait_for=None):
        """
        :arg queue: a :class:`pyopencl.CommandQueue`
        :arg tree: a :class:`boxtree.Tree`.
        :arg ball_centers: an object array of coordinate
            :class:`pyopencl.array.Array` instances.
            Their *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg ball_radii: a
            :class:`pyopencl.array.Array`
            of positive numbers.
            Its *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg peer_lists: may either be *None* or an instance of
            :class:`PeerListLookup` associated with `tree`.
        :arg wait_for: may either be *None* or a list of :class:`pyopencl.Event`
            instances for whose completion this command waits before starting
            exeuction.
        :returns: a tuple *(aq, event)*, where *aq* is an instance of
            :class:`AreaQueryResult`, and *event* is a :class:`pyopencl.Event`
            for dependency management.
        """

        from pytools import single_valued
        if single_valued(bc.dtype for bc in ball_centers) != tree.coord_dtype:
            raise TypeError("ball_centers dtype must match tree.coord_dtype")
        if ball_radii.dtype != tree.coord_dtype:
            raise TypeError("ball_radii dtype must match tree.coord_dtype")

        ball_id_dtype = tree.particle_id_dtype  # ?

        from pytools import div_ceil
        # Avoid generating too many kernels.
        max_levels = div_ceil(tree.nlevels, 10) * 10

        if peer_lists is None:
            peer_lists, evt = self.peer_list_finder(queue, tree, wait_for=wait_for)
            wait_for = [evt]

        if len(peer_lists.peer_list_starts) != tree.nboxes + 1:
            raise ValueError("size of peer lists must match with number of boxes")

        area_query_kernel = self.get_area_query_kernel(tree.dimensions,
            tree.coord_dtype, tree.box_id_dtype, ball_id_dtype,
            peer_lists.peer_list_starts.dtype, max_levels)

        logger.info("area query: run area query")

        result, evt = area_query_kernel(
                queue, len(ball_radii),
                tree.box_centers.data, tree.root_extent,
                tree.box_levels.data, tree.aligned_nboxes,
                tree.box_child_ids.data, tree.box_flags.data,
                peer_lists.peer_list_starts.data,
                peer_lists.peer_lists.data, ball_radii.data,
                *(tuple(tree.bounding_box[0]) +
                  tuple(bc.data for bc in ball_centers)),
                wait_for=wait_for)

        logger.info("area query: done")

        return AreaQueryResult(
                tree=tree,
                leaves_near_ball_starts=result["leaves"].starts,
                leaves_near_ball_lists=result["leaves"].lists).with_queue(None), evt

# }}}


# {{{ area query transpose (leaves-to-balls) lookup build

class LeavesToBallsLookupBuilder(object):
    """Given a set of :math:`l^\infty` "balls", this class helps build a
    look-up table from leaf boxes to balls that overlap with each leaf box.

    .. automethod:: __call__

    """
    def __init__(self, context):
        self.context = context

        from pyopencl.algorithm import KeyValueSorter
        self.key_value_sorter = KeyValueSorter(context)
        self.area_query_builder = AreaQueryBuilder(context)

    @memoize_method
    def get_starts_expander_kernel(self, idx_dtype):
        """
        Expands a "starts" array into a length starts[-1] array of increasing
        indices:

        Eg: [0 2 5 6] => [0 0 1 1 1 2]

        """
        return STARTS_EXPANDER_TEMPLATE.build(
                self.context,
                type_aliases=(("idx_t", idx_dtype),))

    def __call__(self, queue, tree, ball_centers, ball_radii, peer_lists=None,
                 wait_for=None):
        """
        :arg queue: a :class:`pyopencl.CommandQueue`
        :arg tree: a :class:`boxtree.Tree`.
        :arg ball_centers: an object array of coordinate
            :class:`pyopencl.array.Array` instances.
            Their *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg ball_radii: a
            :class:`pyopencl.array.Array`
            of positive numbers.
            Its *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg peer_lists: may either be *None* or an instance of
            :class:`PeerListLookup` associated with `tree`.
        :arg wait_for: may either be *None* or a list of :class:`pyopencl.Event`
            instances for whose completion this command waits before starting
            execution.
        :returns: a tuple *(lbl, event)*, where *lbl* is an instance of
            :class:`LeavesToBallsLookup`, and *event* is a :class:`pyopencl.Event`
            for dependency management.
        """

        from pytools import single_valued
        if single_valued(bc.dtype for bc in ball_centers) != tree.coord_dtype:
            raise TypeError("ball_centers dtype must match tree.coord_dtype")
        if ball_radii.dtype != tree.coord_dtype:
            raise TypeError("ball_radii dtype must match tree.coord_dtype")

        logger.info("leaves-to-balls lookup: run area query")

        area_query, evt = self.area_query_builder(
                queue, tree, ball_centers, ball_radii, peer_lists, wait_for)
        wait_for = [evt]

        logger.info("leaves-to-balls lookup: expand starts")

        nkeys = len(area_query.leaves_near_ball_lists)
        nballs_p_1 = len(area_query.leaves_near_ball_starts)
        assert nballs_p_1 == len(ball_radii) + 1

        starts_expander_knl = self.get_starts_expander_kernel(tree.box_id_dtype)
        expanded_starts = cl.array.empty(queue, nkeys, tree.box_id_dtype)
        evt = starts_expander_knl(
            expanded_starts,
            area_query.leaves_near_ball_starts.with_queue(queue),
            nballs_p_1)
        wait_for = [evt]

        logger.info("leaves-to-balls lookup: key-value sort")

        balls_near_box_starts, balls_near_box_lists, evt \
                = self.key_value_sorter(
                        queue,
                        # keys
                        area_query.leaves_near_ball_lists.with_queue(queue),
                        # values
                        expanded_starts,
                        nkeys, starts_dtype=tree.box_id_dtype,
                        wait_for=wait_for)

        logger.info("leaves-to-balls lookup: built")

        return LeavesToBallsLookup(
                tree=tree,
                balls_near_box_starts=balls_near_box_starts,
                balls_near_box_lists=balls_near_box_lists).with_queue(None), evt

# }}}


# {{{ space invader query build

class SpaceInvaderQueryBuilder(object):
    """Given a set of :math:`l^\infty` "balls", this class helps build a look-up
    table from leaf box to max center-to-center distance with an overlapping
    ball.

    .. automethod:: __call__

    """
    def __init__(self, context):
        self.context = context
        self.peer_list_finder = PeerListFinder(self.context)

    # {{{ Kernel generation

    @memoize_method
    def get_space_invader_query_kernel(self, dimensions, coord_dtype,
                box_id_dtype, peer_list_idx_dtype, max_levels):
        return SPACE_INVADER_QUERY_TEMPLATE.generate(
                self.context,
                dimensions,
                coord_dtype,
                box_id_dtype,
                peer_list_idx_dtype,
                max_levels)

    # }}}

    def __call__(self, queue, tree, ball_centers, ball_radii, peer_lists=None,
                 wait_for=None):
        """
        :arg queue: a :class:`pyopencl.CommandQueue`
        :arg tree: a :class:`boxtree.Tree`.
        :arg ball_centers: an object array of coordinate
            :class:`pyopencl.array.Array` instances.
            Their *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg ball_radii: a
            :class:`pyopencl.array.Array`
            of positive numbers.
            Its *dtype* must match *tree*'s
            :attr:`boxtree.Tree.coord_dtype`.
        :arg peer_lists: may either be *None* or an instance of
            :class:`PeerListLookup` associated with `tree`.
        :arg wait_for: may either be *None* or a list of :class:`pyopencl.Event`
            instances for whose completion this command waits before starting
            execution.
        :returns: a tuple *(sqi, event)*, where *sqi* is an instance of
            :class:`pyopencl.array`, and *event* is a :class:`pyopencl.Event`
            for dependency management.
        """

        from pytools import single_valued
        if single_valued(bc.dtype for bc in ball_centers) != tree.coord_dtype:
            raise TypeError("ball_centers dtype must match tree.coord_dtype")
        if ball_radii.dtype != tree.coord_dtype:
            raise TypeError("ball_radii dtype must match tree.coord_dtype")

        from pytools import div_ceil
        # Avoid generating too many kernels.
        max_levels = div_ceil(tree.nlevels, 10) * 10

        if peer_lists is None:
            peer_lists, evt = self.peer_list_finder(queue, tree, wait_for=wait_for)
            wait_for = [evt]

        if len(peer_lists.peer_list_starts) != tree.nboxes + 1:
            raise ValueError("size of peer lists must match with number of boxes")

        space_invader_query_kernel = self.get_space_invader_query_kernel(
            tree.dimensions, tree.coord_dtype, tree.box_id_dtype,
            peer_lists.peer_list_starts.dtype, max_levels)

        logger.info("space invader query: run space invader query")

        outer_space_invader_dists = cl.array.zeros(queue, tree.nboxes, np.float32)
        if not wait_for:
            wait_for = []
        wait_for = wait_for + outer_space_invader_dists.events

        evt = space_invader_query_kernel(
                *SPACE_INVADER_QUERY_TEMPLATE.unwrap_args(
                    tree, peer_lists,
                    ball_radii,
                    outer_space_invader_dists,
                    *tuple(bc for bc in ball_centers)),
                wait_for=wait_for,
                queue=queue,
                slice=slice(len(ball_radii)))

        if tree.coord_dtype != np.dtype(np.float32):
            # The kernel output is always an array of float32 due to limited
            # support for atomic operations with float64 in OpenCL.
            # Here the output is cast to match the coord dtype.
            outer_space_invader_dists.finish()
            outer_space_invader_dists = outer_space_invader_dists.astype(
                    tree.coord_dtype)
            evt, = outer_space_invader_dists.events

        logger.info("space invader query: done")

        return outer_space_invader_dists, evt

# }}}


# {{{ peer list build


class PeerListFinder(object):
    """This class builds a look-up table from box numbers to peer boxes. The
    full definition [1]_ of a peer box is as follows:

        Given a box :math:`b_j` in a quad-tree, :math:`b_k` is a peer box of
        :math:`b_j` if it is

         1. adjacent to :math:`b_j`,

         2. of at least the same size as :math:`b_j` (i.e. at the same or a
            higher level than), and

         3. no child of :math:`b_k` satisfies the above two criteria.

    .. [1] Rachh, Manas, Andreas Klöckner, and Michael O'Neil. "Fast
       algorithms for Quadrature by Expansion I: Globally valid expansions."

    .. versionadded:: 2016.1

    .. automethod:: __call__
    """

    def __init__(self, context):
        self.context = context

    # {{{ Kernel generation

    @memoize_method
    def get_peer_list_finder_kernel(self, dimensions, coord_dtype,
                                    box_id_dtype, max_levels):
        from pyopencl.tools import dtype_to_ctype
        from boxtree import box_flags_enum

        logger.info("start building peer list finder kernel")

        from boxtree.traversal import (
            TRAVERSAL_PREAMBLE_TEMPLATE, HELPER_FUNCTION_TEMPLATE)

        template = Template(
            TRAVERSAL_PREAMBLE_TEMPLATE
            + HELPER_FUNCTION_TEMPLATE
            + PEER_LIST_FINDER_TEMPLATE,
            strict_undefined=True)

        render_vars = dict(
            dimensions=dimensions,
            dtype_to_ctype=dtype_to_ctype,
            box_id_dtype=box_id_dtype,
            particle_id_dtype=None,
            coord_dtype=coord_dtype,
            vec_types=cl.array.vec.types,
            max_levels=max_levels,
            AXIS_NAMES=AXIS_NAMES,
            box_flags_enum=box_flags_enum,
            debug=False,
            # Not used (but required by TRAVERSAL_PREAMBLE_TEMPLATE)
            stick_out_factor=0,
            # For calls to the helper is_adjacent_or_overlapping()
            targets_have_extent=False,
            sources_have_extent=False)

        from pyopencl.tools import VectorArg, ScalarArg
        arg_decls = [
            VectorArg(coord_dtype, "box_centers"),
            ScalarArg(coord_dtype, "root_extent"),
            VectorArg(np.uint8, "box_levels"),
            ScalarArg(box_id_dtype, "aligned_nboxes"),
            VectorArg(box_id_dtype, "box_child_ids"),
            VectorArg(box_flags_enum.dtype, "box_flags"),
        ]

        from pyopencl.algorithm import ListOfListsBuilder
        peer_list_finder_kernel = ListOfListsBuilder(
            self.context,
            [("peers", box_id_dtype)],
            str(template.render(**render_vars)),
            arg_decls=arg_decls,
            name_prefix="find_peer_lists",
            count_sharing={},
            complex_kernel=True)

        logger.info("done building peer list finder kernel")
        return peer_list_finder_kernel

    # }}}

    def __call__(self, queue, tree, wait_for=None):
        """
        :arg queue: a :class:`pyopencl.CommandQueue`
        :arg tree: a :class:`boxtree.Tree`.
        :arg wait_for: may either be *None* or a list of :class:`pyopencl.Event`
            instances for whose completion this command waits before starting
            execution.
        :returns: a tuple *(pl, event)*, where *pl* is an instance of
            :class:`PeerListLookup`, and *event* is a :class:`pyopencl.Event`
            for dependency management.
        """
        from pytools import div_ceil
        # Avoid generating too many kernels.
        max_levels = div_ceil(tree.nlevels, 10) * 10

        peer_list_finder_kernel = self.get_peer_list_finder_kernel(
            tree.dimensions, tree.coord_dtype, tree.box_id_dtype, max_levels)

        logger.info("peer list finder: find peer lists")

        result, evt = peer_list_finder_kernel(
                queue, tree.nboxes,
                tree.box_centers.data, tree.root_extent,
                tree.box_levels.data, tree.aligned_nboxes,
                tree.box_child_ids.data, tree.box_flags.data,
                wait_for=wait_for)

        logger.info("peer list finder: done")

        return PeerListLookup(
                tree=tree,
                peer_list_starts=result["peers"].starts,
                peer_lists=result["peers"].lists).with_queue(None), evt

# }}}

# vim: filetype=pyopencl:fdm=marker
